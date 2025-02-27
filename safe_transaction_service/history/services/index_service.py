import logging
from typing import Collection, List, Optional, OrderedDict, Union

from django.db import IntegrityError, transaction

from eth_typing import ChecksumAddress
from hexbytes import HexBytes

from gnosis.eth import EthereumClient, EthereumClientProvider

from ..models import (
    EthereumBlock,
    EthereumTx,
    InternalTxDecoded,
    ModuleTransaction,
    MultisigConfirmation,
    MultisigTransaction,
    SafeStatus,
)

logger = logging.getLogger(__name__)


class IndexingException(Exception):
    pass


class TransactionNotFoundException(IndexingException):
    pass


class TransactionWithoutBlockException(IndexingException):
    pass


class BlockNotFoundException(IndexingException):
    pass


class IndexServiceProvider:
    def __new__(cls):
        if not hasattr(cls, "instance"):
            from django.conf import settings

            cls.instance = IndexService(
                EthereumClientProvider(),
                settings.ETH_REORG_BLOCKS,
                settings.ETH_L2_NETWORK,
            )
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


# TODO Test IndexService
class IndexService:
    def __init__(
        self,
        ethereum_client: EthereumClient,
        eth_reorg_blocks: int,
        eth_l2_network: bool,
    ):
        self.ethereum_client = ethereum_client
        self.eth_reorg_blocks = eth_reorg_blocks
        self.eth_l2_network = eth_l2_network

    def block_get_or_create_from_block_hash(self, block_hash: int):
        try:
            return EthereumBlock.objects.get(block_hash=block_hash)
        except EthereumBlock.DoesNotExist:
            current_block_number = (
                self.ethereum_client.current_block_number
            )  # For reorgs
            block = self.ethereum_client.get_block(block_hash)
            confirmed = (
                current_block_number - block["number"]
            ) >= self.eth_reorg_blocks
            return EthereumBlock.objects.get_or_create_from_block(
                block, confirmed=confirmed
            )

    def tx_create_or_update_from_tx_hash(self, tx_hash: str) -> "EthereumTx":
        try:
            ethereum_tx = EthereumTx.objects.get(tx_hash=tx_hash)
            # For txs stored before being mined
            if ethereum_tx.block is None:
                tx_receipt = self.ethereum_client.get_transaction_receipt(tx_hash)
                ethereum_block = self.block_get_or_create_from_block_hash(
                    tx_receipt["blockHash"]
                )
                ethereum_tx.update_with_block_and_receipt(ethereum_block, tx_receipt)
            return ethereum_tx
        except EthereumTx.DoesNotExist:
            tx_receipt = self.ethereum_client.get_transaction_receipt(tx_hash)
            ethereum_block = self.block_get_or_create_from_block_hash(
                tx_receipt["blockHash"]
            )
            tx = self.ethereum_client.get_transaction(tx_hash)
            return EthereumTx.objects.create_from_tx_dict(
                tx, tx_receipt=tx_receipt, ethereum_block=ethereum_block
            )

    def txs_create_or_update_from_tx_hashes(
        self, tx_hashes: Collection[Union[str, bytes]]
    ) -> List["EthereumTx"]:
        # Search first in database
        ethereum_txs_dict = OrderedDict.fromkeys(
            [HexBytes(tx_hash).hex() for tx_hash in tx_hashes]
        )
        db_ethereum_txs = EthereumTx.objects.filter(tx_hash__in=tx_hashes).exclude(
            block=None
        )
        for db_ethereum_tx in db_ethereum_txs:
            ethereum_txs_dict[db_ethereum_tx.tx_hash] = db_ethereum_tx

        # Retrieve from the node the txs missing from database
        tx_hashes_not_in_db = [
            tx_hash
            for tx_hash, ethereum_tx in ethereum_txs_dict.items()
            if not ethereum_tx
        ]
        if not tx_hashes_not_in_db:
            return list(ethereum_txs_dict.values())

        self.ethereum_client = EthereumClientProvider()

        # Get receipts for hashes not in db
        tx_receipts = []
        for tx_hash, tx_receipt in zip(
            tx_hashes_not_in_db,
            self.ethereum_client.get_transaction_receipts(tx_hashes_not_in_db),
        ):
            tx_receipt = tx_receipt or self.ethereum_client.get_transaction_receipt(
                tx_hash
            )  # Retry fetching if failed
            if not tx_receipt:
                raise TransactionNotFoundException(
                    f"Cannot find tx-receipt with tx-hash={HexBytes(tx_hash).hex()}"
                )
            elif tx_receipt.get("blockNumber") is None:
                raise TransactionWithoutBlockException(
                    f"Cannot find blockNumber for tx-receipt with "
                    f"tx-hash={HexBytes(tx_hash).hex()}"
                )
            else:
                tx_receipts.append(tx_receipt)

        # Get transactions for hashes not in db
        fetched_txs = self.ethereum_client.get_transactions(tx_hashes_not_in_db)
        block_hashes = set()
        txs = []
        for tx_hash, tx in zip(tx_hashes_not_in_db, fetched_txs):
            tx = tx or self.ethereum_client.get_transaction(
                tx_hash
            )  # Retry fetching if failed
            if not tx:
                raise TransactionNotFoundException(
                    f"Cannot find tx with tx-hash={HexBytes(tx_hash).hex()}"
                )
            elif tx.get("blockHash") is None:
                raise TransactionWithoutBlockException(
                    f"Cannot find blockHash for tx with "
                    f"tx-hash={HexBytes(tx_hash).hex()}"
                )
            block_hashes.add(tx["blockHash"].hex())
            txs.append(tx)

        blocks = self.ethereum_client.get_blocks(block_hashes)
        block_dict = {}
        for block_hash, block in zip(block_hashes, blocks):
            block = block or self.ethereum_client.get_block(
                block_hash
            )  # Retry fetching if failed
            if not block:
                raise BlockNotFoundException(
                    f"Block with hash={block_hash} was not found"
                )
            assert block_hash == block["hash"].hex()
            block_dict[block["hash"]] = block

        # Create new transactions or update them if they have no receipt
        current_block_number = self.ethereum_client.current_block_number
        for tx, tx_receipt in zip(txs, tx_receipts):
            block = block_dict[tx["blockHash"]]
            confirmed = (
                current_block_number - block["number"]
            ) >= self.eth_reorg_blocks
            ethereum_block: EthereumBlock = (
                EthereumBlock.objects.get_or_create_from_block(
                    block, confirmed=confirmed
                )
            )
            try:
                with transaction.atomic():
                    ethereum_tx = EthereumTx.objects.create_from_tx_dict(
                        tx, tx_receipt=tx_receipt, ethereum_block=ethereum_block
                    )
                ethereum_txs_dict[HexBytes(ethereum_tx.tx_hash).hex()] = ethereum_tx
            except IntegrityError:  # Tx exists
                ethereum_tx = EthereumTx.objects.get(tx_hash=tx["hash"])
                # For txs stored before being mined
                ethereum_tx.update_with_block_and_receipt(ethereum_block, tx_receipt)
                ethereum_txs_dict[ethereum_tx.tx_hash] = ethereum_tx
        return list(ethereum_txs_dict.values())

    @transaction.atomic
    def _reprocess(self, addresses: List[str]):
        """
        Trigger processing of traces again. If addresses is empty, everything is reprocessed

        :param addresses:
        :return:
        """
        queryset = MultisigConfirmation.objects.filter(signature=None)
        if not addresses:
            logger.info("Remove onchain confirmations")
            queryset.delete()

        logger.info("Remove transactions automatically indexed")
        queryset = MultisigTransaction.objects.exclude(ethereum_tx=None)
        if addresses:
            queryset = queryset.filter(safe__in=addresses)
        queryset.delete()

        logger.info("Remove module transactions")
        queryset = ModuleTransaction.objects.all()
        if addresses:
            queryset = queryset.filter(safe__in=addresses)
        queryset.delete()

        logger.info("Remove Safe statuses")

        queryset = SafeStatus.objects.all()
        if addresses:
            queryset = queryset.filter(address__in=addresses)
        queryset.delete()

        logger.info("Mark all internal txs decoded as not processed")
        queryset = InternalTxDecoded.objects.all()
        if addresses:
            queryset = queryset.filter(internal_tx___from__in=addresses)
        queryset.update(processed=False)

    def reprocess_addresses(self, addresses: List[str]):
        """
        Given a list of safe addresses it will delete all `SafeStatus`, conflicting `MultisigTxs` and will mark
        every `InternalTxDecoded` not processed to be processed again

        :param addresses: List of checksummed addresses or queryset
        :return: Number of `SafeStatus` deleted
        """
        if not addresses:
            return

        return self._reprocess(addresses)

    def reprocess_all(self):
        return self._reprocess(None)

    def reindex_master_copies(
        self,
        from_block_number: int,
        to_block_number: Optional[int] = None,
        block_process_limit: int = 100,
        addresses: Optional[ChecksumAddress] = None,
    ):
        """
        Reindexes master copies in parallel with the current running indexer, so service will have no missing txs
        while reindexing

        :param from_block_number: Block number to start indexing from
        :param to_block_number: Block number to stop indexing on
        :param block_process_limit: Number of blocks to process each time
        :param addresses: Master Copy or Safes(for L2 event processing) addresses. If not provided,
            all master copies will be used
        """
        assert (not to_block_number) or to_block_number > from_block_number

        from ..indexers import (
            EthereumIndexer,
            InternalTxIndexerProvider,
            SafeEventsIndexerProvider,
        )

        indexer_provider = (
            SafeEventsIndexerProvider
            if self.eth_l2_network
            else InternalTxIndexerProvider
        )
        indexer: EthereumIndexer = indexer_provider()
        ethereum_client = EthereumClientProvider()

        if addresses:
            indexer.IGNORE_ADDRESSES_ON_LOG_FILTER = (
                False  # Just process addresses provided
            )
        else:
            addresses = list(
                indexer.database_queryset.values_list("address", flat=True)
            )

        if not addresses:
            logger.warning("No addresses to process")
        else:
            logger.info("Start reindexing Safe Master Copy addresses %s", addresses)
            current_block_number = ethereum_client.current_block_number
            stop_block_number = (
                min(current_block_number, to_block_number)
                if to_block_number
                else current_block_number
            )
            block_number = from_block_number
            while block_number < stop_block_number:
                elements = indexer.find_relevant_elements(
                    addresses, block_number, block_number + block_process_limit
                )
                indexer.process_elements(elements)
                block_number += block_process_limit
                logger.info(
                    "Current block number %d, found %d traces/events",
                    block_number,
                    len(elements),
                )

            logger.info("End reindexing addresses %s", addresses)
