import asyncio
import random

from bittensor.core.chain_data.utils import decode_account_id

from patrol.chain_data.substrate_client import SubstrateClient
from patrol.chain_data.runtime_groupings import get_version_for_block
from patrol.constants import Constants
import logging

logger = logging.getLogger(__name__)
class HotkeyTargetGenerator:
    def __init__(self, substrate_client: SubstrateClient):
        self.substrate_client = substrate_client
        self.runtime_versions = self.substrate_client.return_runtime_versions()

    @staticmethod
    def format_address(addr) -> str:
        """
        Uses Bittensor's decode_account_id to format the given address.
        Assumes 'addr' is provided in the format expected by decode_account_id.
        """
        try:
            return decode_account_id(addr)
        except Exception as e:
            return addr

    async def generate_random_block_numbers(self, num_blocks: int, current_block: int) -> list[int]:
        # start_block = random.randint(Constants.LOWER_BLOCK_LIMIT, current_block - num_blocks * 4 * 600)
        start_block = random.randint(Constants.DTAO_RELEASE_BLOCK, current_block - num_blocks * 4 * 600)
        return [start_block + i * 500 for i in range(num_blocks * 4)]

    async def fetch_subnets_and_owners(self, block, current_block):
        block_hash = await self.substrate_client.query("get_block_hash", None, block)
        ver = get_version_for_block(block, current_block, self.runtime_versions)

        subnets = []

        result = await self.substrate_client.query(
            "query_map",
            ver,
            "SubtensorModule",
            "NetworksAdded",
            params=None,
            block_hash=block_hash
        )

        async for netuid, exists in result:
            if exists.value:
                subnets.append((block, netuid))

        subnet_owners = set()
        for _, netuid in subnets:
            owner = await self.substrate_client.query(
                "query",
                ver,
                "SubtensorModule",
                "SubnetOwner",
                [netuid],
                block_hash=block_hash
            )
            subnet_owners.add(owner)

        return subnets, subnet_owners
    
    async def query_metagraph_direct(self, block_number: int, netuid: int, current_block: int):
        # Get the block hash for the specific block
        block_hash = await self.substrate_client.query("get_block_hash", None, block_number)
        # Get the runtime version for this block
        ver = get_version_for_block(block_number, current_block, self.runtime_versions)

        # Make the runtime API call
        raw = await self.substrate_client.query(
            "runtime_call",
            ver,
            "NeuronInfoRuntimeApi",
            "get_neurons_lite",
            block_hash=block_hash,
            params=[netuid]
        )
        
        return raw.decode()

    async def generate_targets(
    self, max_block_number: int, num_targets: int = 10
) -> list[str]:
        """
        This function aims to generate target hotkeys from active participants in the ecosystem.
        """
        max_block_number = 5400000
        block_numbers = [i for i in range(4920351, max_block_number)]

        target_hotkeys = set()

        # Process blocks in batches of 10
        batch_size = 10
        subnet_batch_size = 10

        logger.info(f"Processing {len(block_numbers)} blocks in batches of {batch_size}")

        for i in range(0, len(block_numbers), batch_size):
            batch = block_numbers[i : i + batch_size]
            logger.info(
                f"Processing block batch {i//batch_size + 1}/{(len(block_numbers) + batch_size - 1)//batch_size} with {len(batch)} blocks"
            )

            subnet_list = []

            # Step 1: Fetch subnets and owners for current block batch
            tasks = [
                self.fetch_subnets_and_owners(block_number, max_block_number)
                for block_number in batch
            ]
            logger.info(f"LEN OF TASKS: {len(tasks)}")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            results = [result for result in results if result is not None]

            for subnets, subnet_owners in results:
                target_hotkeys.update(subnet_owners)
                subnet_list.extend(subnets)

            logger.info(f"LEN OF SUBNET LIST FOR THIS BLOCK BATCH: {len(subnet_list)}")

            # Step 2: Process subnets in batches of 10
            if subnet_list:
                for j in range(0, len(subnet_list), subnet_batch_size):
                    subnet_batch = subnet_list[j : j + subnet_batch_size]
                    logger.info(
                        f"Processing subnet batch {j//subnet_batch_size + 1}/{(len(subnet_list) + subnet_batch_size - 1)//subnet_batch_size} with {len(subnet_batch)} subnets"
                    )

                    tasks = [
                        self.query_metagraph_direct(
                            block_number=subnet[0],
                            netuid=subnet[1],
                            current_block=max_block_number,
                        )
                        for subnet in subnet_batch
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    results = [result for result in results if result is not None]

                    for neurons in results:
                        hotkeys = [
                            self.format_address(neuron["hotkey"]) for neuron in neurons
                        ]
                        target_hotkeys.update(hotkeys)

                    # Check existing hotkeys from file each time before writing
                    existing_hotkeys = set()
                    try:
                        with open("target_hotkeys.txt", "r") as f:
                            existing_hotkeys = set(
                                line.strip() for line in f if line.strip()
                            )
                    except FileNotFoundError:
                        pass  # File doesn't exist yet, that's fine

                    # Write only new hotkeys to file after each subnet batch
                    new_hotkeys = target_hotkeys - existing_hotkeys
                    if new_hotkeys:
                        with open("target_hotkeys.txt", "a") as f:
                            f.write("\n".join(new_hotkeys) + "\n")
                        logger.info(
                            f"Written {len(new_hotkeys)} new hotkeys to file after subnet batch {j//subnet_batch_size + 1}"
                        )
                    else:
                        logger.info(
                            f"No new hotkeys found in subnet batch {j//subnet_batch_size + 1}"
                        )

            logger.info(
                f"Completed block batch {i//batch_size + 1}. Total hotkeys collected so far: {len(target_hotkeys)}"
            )
        logger.info(f"GET TARGET HOTKEYS: {len(target_hotkeys)} DONE")

        target_hotkeys = list(target_hotkeys)
        random.shuffle(target_hotkeys)
        return target_hotkeys[:num_targets]

if __name__ == "__main__":
    import time
    from patrol.chain_data.runtime_groupings import load_versions

    async def example():
        network_url = "wss://archive.chain.opentensor.ai:443/"
        versions = load_versions()
        # only keep the runtime we care about
        versions = {k: versions[k] for k in versions.keys() if int(k) == 149}

        # version = get_version_for_block(3014350, 5014352, versions)
        # print(version)

        client = SubstrateClient(
            runtime_mappings=versions,
            network_url=network_url,
        )
        await client.initialize()

        start_time = time.time()

        selector = HotkeyTargetGenerator(substrate_client=client, runtime_versions=versions)
        hotkey_addresses = await selector.generate_targets(num_targets=256)        
        
        end_time = time.time()
        print(f"Time taken: {end_time - start_time} seconds")

        print(f"\nSelected {len(hotkey_addresses)} hotkey addresses.")

    asyncio.run(example())