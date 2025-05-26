import asyncio
import logging
import random
import uuid

from bittensor.core.metagraph import AsyncMetagraph

from patrol.validation.chain.chain_reader import ChainReader
from patrol.validation.hotkey_ownership.hotkey_ownership_challenge import HotkeyOwnershipChallenge, Miner
from patrol.validation.hotkey_ownership.hotkey_target_generation import HotkeyTargetGenerator

logger = logging.getLogger(__name__)

class HotkeyOwnershipBatch:

    def __init__(self,
                 challenge: HotkeyOwnershipChallenge,
                 target_generator: HotkeyTargetGenerator,
                 metagraph: AsyncMetagraph,
                 chain_reader: ChainReader,
                 concurrency: int
     ):
        self.challenge = challenge
        self.target_generator = target_generator
        self.metagraph = metagraph
        self.chain_reader = chain_reader
        self.concurrency_semaphore = asyncio.Semaphore(concurrency)

    async def challenge_miners(self):

        current_block = await self.chain_reader.get_current_block()
        max_block_number = current_block - 10

        batch_id = uuid.uuid4()
        logging_extra = {"batch_id": str(batch_id)}

        logger.info("Batch started", extra=logging_extra)

        axons = self.metagraph.axons
        uids = self.metagraph.uids.tolist()

        miners = list(filter(
            lambda m: m.axon_info.is_serving,
            (Miner(axon, uids[idx]) for idx, axon in enumerate(axons))
        ))

        target_hotkeys = await self.target_generator.generate_targets(max_block_number, len(miners))
        miner_target_hotkeys = {miner.uid: target_hotkeys.pop() for miner in miners}

        random.shuffle(miners)

        async def challenge(miner):
            try:
                async with self.concurrency_semaphore:
                    await self.challenge.execute_challenge(miner, miner_target_hotkeys[miner.uid], batch_id, max_block_number)
            except Exception as ex:
                logger.exception("Unhandled error: %s", ex)

        challenge_tasks = [challenge(miner) for miner in miners]
        await asyncio.gather(*challenge_tasks)

        logger.info("Batch completed", extra=logging_extra)

        return batch_id
