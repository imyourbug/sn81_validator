import asyncio
import dataclasses
import logging
import math
import multiprocessing
from datetime import datetime, UTC
from multiprocessing import Semaphore

from async_substrate_interface import AsyncSubstrateInterface
from bittensor_wallet import Wallet
from sqlalchemy.ext.asyncio import create_async_engine

from patrol.validation import TaskType, hooks
from patrol.validation.aws_rds import consume_db_engine
from patrol.validation.chain.chain_reader import ChainReader
from patrol.validation.dashboard import DashboardClient
from patrol.validation.hooks import HookType
from patrol.validation.http_.HttpDashboardClient import HttpDashboardClient
from patrol.validation.persistence.alpha_sell_challenge_repository import DatabaseAlphaSellChallengeRepository
from patrol.validation.persistence.alpha_sell_event_repository import DataBaseAlphaSellEventRepository
from patrol.validation.persistence.miner_score_repository import DatabaseMinerScoreRepository
from patrol.validation.persistence.transaction_helper import TransactionHelper
from patrol.validation.predict_alpha_sell import AlphaSellChallengeTask, AlphaSellChallengeRepository, \
    AlphaSellEventRepository, AlphaSellChallengeBatch, TransactionType
from patrol.validation.scoring import MinerScore, MinerScoreRepository
from patrol_common import WalletIdentifier

logger = logging.getLogger(__name__)


def make_miner_score(task: AlphaSellChallengeTask, stake_removal_score: float, stake_addition_score: float, scoring_batch: int) -> MinerScore:
    overall_score = stake_removal_score + stake_addition_score
    return MinerScore(
        id=task.task_id, batch_id=task.batch_id, created_at=datetime.now(UTC),
        uid=task.miner.uid,
        coldkey=task.miner.coldkey,
        hotkey=task.miner.hotkey,
        responsiveness_score=0.0,
        accuracy_score=overall_score,
        volume=0,
        volume_score=0.0,
        response_time_seconds=0.0,
        novelty_score=0.0,
        validation_passed=not task.has_error,
        error_message=task.error_message,
        task_type=TaskType.PREDICT_ALPHA_SELL,
        overall_score=overall_score,
        overall_score_moving_average=0.0,
        scoring_batch=scoring_batch,
        stake_removal_score=stake_removal_score,
        stake_addition_score=stake_addition_score,
    )


class AlphaSellValidator:

    def __init__(self, noise_floor: float = 1.0, steepness = 2.0):
        self.noise_floor = noise_floor
        self.steepness = steepness

    def score_miner_accuracy(self, task: AlphaSellChallengeTask, stake_movements: dict[WalletIdentifier, int], transaction_type: TransactionType) -> float:
        if task.has_error:
            return 0.0

        predictions_by_wallet = {WalletIdentifier(p.wallet_coldkey_ss58, p.wallet_hotkey_ss58): p.amount for p in task.predictions if p.transaction_type == transaction_type}

        accuracies = []

        for hk in predictions_by_wallet.keys():
            predicted_rao = predictions_by_wallet[hk]
            predicted_tao = predicted_rao / 1e9

            actual_rao = stake_movements.get(hk, 0)
            actual_tao = actual_rao / 1e9

            relative_delta = (self.steepness * (predicted_tao - actual_tao) / (actual_tao + self.noise_floor)) ** 2

            movement_size_factor = 1 + math.log10(actual_tao + self.noise_floor)

            accuracy = movement_size_factor * max(0.0, 1.0 - relative_delta)
            accuracies.append(accuracy)

        if len(accuracies) == 0:
            return 0.0

        return sum(accuracies)


class AlphaSellScoring:

    def __init__(
            self, challenge_repository: AlphaSellChallengeRepository,
            miner_score_repository: MinerScoreRepository,
            chain_reader: ChainReader,
            alpha_sell_event_repository: AlphaSellEventRepository,
            alpha_sell_validator: AlphaSellValidator,
            dashboard_client: DashboardClient | None,
            transaction_helper: TransactionHelper,
    ):
        self.challenge_repository = challenge_repository
        self.miner_score_repository = miner_score_repository
        self.chain_reader = chain_reader
        self.alpha_sell_event_repository = alpha_sell_event_repository
        self.alpha_sell_validator = alpha_sell_validator
        self.dashboard_client = dashboard_client
        self.transaction_helper = transaction_helper

    async def score_miners(self):
        upper_block = (await self.chain_reader.get_current_block()) - 1
        scorable_batches = await self.challenge_repository.find_scorable_challenges(upper_block)

        if len(scorable_batches) == 0:
            logger.info("No scorable batches found")

        for scorable_challenge_batch in scorable_batches:
            await self._score_batch(scorable_challenge_batch)

    async def _score_batch(self, batch: AlphaSellChallengeBatch):
        logger.info("Scoring batch [%s]", batch.batch_id)
        stake_removals = await self.alpha_sell_event_repository.find_aggregate_stake_movement_by_wallet(
            subnet_id=batch.subnet_uid,
            lower_block=batch.prediction_interval.start_block, upper_block=batch.prediction_interval.end_block,
            transaction_type=TransactionType.STAKE_REMOVED
        )
        stake_additions = await self.alpha_sell_event_repository.find_aggregate_stake_movement_by_wallet(
            subnet_id=batch.subnet_uid,
            lower_block=batch.prediction_interval.start_block, upper_block=batch.prediction_interval.end_block,
            transaction_type=TransactionType.STAKE_ADDED
        )
        logger.info("Found %s stake removals", len(stake_removals))
        logger.info("Found %s stake additions", len(stake_additions))

        scorable_tasks = await self.challenge_repository.find_tasks(batch.batch_id)
        logger.info("Found %s scorable tasks in batch %s", len(scorable_tasks), batch.batch_id)
        scores = []

        for task in scorable_tasks:
            miner_log_context = dataclasses.asdict(task.miner)
            logger.info("Scoring task id [%s] in subnet [%s] with [%s] predictions",
                        task.task_id, batch.subnet_uid, len(task.predictions), extra=miner_log_context)
            miner_score = await self._score_task(task, stake_removals, stake_additions, batch.scoring_batch)
            scores.append(miner_score)

        await self.challenge_repository.remove_if_fully_scored(batch.batch_id)
        if self.dashboard_client:
            try:
                await self.dashboard_client.send_scores(scores)
            except Exception:
                logger.exception("Error sending scores to dashboard")


    async def _score_task(self, task: AlphaSellChallengeTask, stake_removals: dict[WalletIdentifier, int], stake_additions: dict[WalletIdentifier, int], scoring_batch: int) -> MinerScore:

        stake_removal_score = self.alpha_sell_validator.score_miner_accuracy(task, stake_removals, TransactionType.STAKE_REMOVED)
        stake_addition_score = self.alpha_sell_validator.score_miner_accuracy(task, stake_additions, TransactionType.STAKE_ADDED)
        miner_score = make_miner_score(task, stake_removal_score, stake_addition_score, scoring_batch)

        async def add_score(session):
            await self.miner_score_repository.add(miner_score, session)
            await self.challenge_repository.mark_task_scored(task.task_id, session)

        await self.transaction_helper.do_in_transaction(add_score)

        logger.info("Scored miner", extra=dataclasses.asdict(miner_score))
        return miner_score


def start_scoring(wallet: Wallet, db_url: str, enable_dashboard_syndication: bool, semaphore: Semaphore):

    from patrol.validation.config import ENABLE_AWS_RDS_IAM
    if ENABLE_AWS_RDS_IAM:
        hooks.add_on_create_db_engine(consume_db_engine)

    async def start_scoring_async():
        from patrol.validation.config import DASHBOARD_BASE_URL, ARCHIVE_SUBTENSOR, SCORING_INTERVAL_SECONDS
        engine = create_async_engine(db_url, pool_pre_ping=True)
        hooks.invoke(HookType.ON_CREATE_DB_ENGINE, engine)

        challenge_repository = DatabaseAlphaSellChallengeRepository(engine)
        alpha_sell_event_repository = DataBaseAlphaSellEventRepository(engine)
        alpha_sell_validator = AlphaSellValidator()
        miner_score_repository = DatabaseMinerScoreRepository(engine)
        dashboard_client = HttpDashboardClient(wallet, DASHBOARD_BASE_URL) if enable_dashboard_syndication else None
        transaction_helper = TransactionHelper(engine)

        async with AsyncSubstrateInterface(ARCHIVE_SUBTENSOR) as substrate:
            chain_utils = ChainReader(substrate)
            scoring = AlphaSellScoring(
                challenge_repository,
                miner_score_repository,
                chain_utils,
                alpha_sell_event_repository,
                alpha_sell_validator,
                dashboard_client,
                transaction_helper
            )

            loop = asyncio.get_running_loop()
            go = True
            while go:
                await loop.run_in_executor(None, semaphore.acquire)
                try:
                    await scoring.score_miners()
                except KeyboardInterrupt:
                    logger.info("Stopping alpha-sell scoring process")
                    go = False
                except Exception as ex:
                    logger.exception("Unexpected error")
                finally:
                    await loop.run_in_executor(None, semaphore.release)
                    await asyncio.sleep(SCORING_INTERVAL_SECONDS)

            logger.info("Stopped alpha-sell scoring process")

    asyncio.run(start_scoring_async())

def start_scoring_process(wallet: Wallet, db_url: str, semaphore: Semaphore, enable_dashboard_syndication: bool = False):
    process = multiprocessing.Process(target=start_scoring, name="Scoring", args=[wallet, db_url, enable_dashboard_syndication, semaphore], daemon=True)
    process.start()
    return process
