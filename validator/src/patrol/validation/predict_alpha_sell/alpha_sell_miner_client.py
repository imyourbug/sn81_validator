import asyncio
import logging
from uuid import UUID

import aiohttp
from aiohttp import TCPConnector
from bittensor import AxonInfo, Dendrite

from patrol.validation.error import MinerTaskException
from patrol.validation.predict_alpha_sell import WalletIdentifier
from patrol_common.protocol import AlphaSellSynapse

logger = logging.getLogger(__name__)

class AlphaSellMinerClient:
    def __init__(self, dendrite: Dendrite, timeout_seconds: float=16.0):
        self._dendrite = dendrite
        self._timeout_seconds = timeout_seconds

    async def execute_tasks(self, miner: AxonInfo, synapses: list[AlphaSellSynapse]) -> list[tuple[UUID, UUID, AlphaSellSynapse]]:

        conn = TCPConnector(limit=len(synapses))
        async with aiohttp.ClientSession(base_url=f"http://{miner.ip}:{miner.port}", connector=conn) as session:
            tasks = [self._execute_task(session, miner, synapse) for synapse in synapses]
            return await asyncio.gather(*tasks, return_exceptions=True)


    async def _execute_task(self, session, miner: AxonInfo, synapse: AlphaSellSynapse) -> tuple[UUID, UUID, AlphaSellSynapse]:
        processed_synapse = self._dendrite.preprocess_synapse_for_request(miner, synapse)

        uri = f"/{synapse.name}"
        headers = processed_synapse.to_headers()
        json_body = processed_synapse.model_dump()

        try:
            response = await session.post(uri, headers=headers, json=json_body, timeout=self._timeout_seconds, ssl=False)
            if not response.ok:
                raise MinerTaskException(
                    f"Error: {response.reason}; status {response.status}",
                    UUID(synapse.task_id),
                    UUID(synapse.batch_id)
                )
            response_synapse = AlphaSellSynapse.model_validate_json(await response.text())

            self._remove_unrequested_hotkey_predictions(response_synapse, synapse.wallets, miner)
            return UUID(synapse.batch_id), UUID(synapse.task_id), response_synapse
        except TimeoutError:
            raise MinerTaskException("Timeout", UUID(synapse.task_id), UUID(synapse.batch_id))
        except Exception as ex:
            raise MinerTaskException(str(ex), UUID(synapse.task_id), UUID(synapse.batch_id))

    def _remove_unrequested_hotkey_predictions(self, synapse: AlphaSellSynapse, wallets: list[WalletIdentifier], miner: AxonInfo):

        if synapse.predictions and synapse.wallets:
            expected_count = 2 * len(synapse.wallets)
            actual_count = len(synapse.predictions)
            if actual_count > expected_count:
                logger.warning(
                    "Miner returned more predictions than expected wallets. "
                    "Expected %d wallets, got %d predictions.",
                    expected_count, actual_count,
                    extra={'batch_id': synapse.batch_id, 'task_id': synapse.task_id, 'miner': miner}
                )

        predictions = [p for p in synapse.predictions if WalletIdentifier(p.wallet_coldkey_ss58, p.wallet_hotkey_ss58) in wallets]
        synapse.predictions = predictions