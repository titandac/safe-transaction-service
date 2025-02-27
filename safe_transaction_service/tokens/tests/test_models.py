from unittest import mock
from unittest.mock import MagicMock

from django.core.exceptions import ValidationError
from django.test import TestCase

from eth_account import Account

from ..clients.zerion_client import (
    BalancerTokenAdapterClient,
    ZerionPoolMetadata,
    ZerionUniswapV2TokenAdapterClient,
)
from ..models import Token
from .factories import TokenFactory


class TestModels(TestCase):
    def test_token_querysets(self):
        erc721_token = TokenFactory(decimals=None)
        self.assertEqual(Token.objects.erc20().count(), 0)
        self.assertEqual(Token.objects.erc721().count(), 1)
        self.assertIn("ERC721", str(erc721_token))

        erc20_token = TokenFactory(decimals=0)
        self.assertEqual(Token.objects.erc20().count(), 1)
        self.assertEqual(Token.objects.erc721().count(), 1)
        self.assertIn("ERC20", str(erc20_token))

        TokenFactory(decimals=4)
        self.assertEqual(Token.objects.erc20().count(), 2)
        self.assertEqual(Token.objects.erc721().count(), 1)
        TokenFactory(decimals=None)
        self.assertEqual(Token.objects.erc20().count(), 2)
        self.assertEqual(Token.objects.erc721().count(), 2)

    def test_token_validation(self):
        t = TokenFactory()
        t.set_spam()
        t.trusted = True
        with self.assertRaises(ValidationError):
            t.clean()

    def test_token_get_full_logo_uri(self):
        t = TokenFactory()
        t.logo_uri = "http://gnosis.io/image.png"
        self.assertEqual(t.get_full_logo_uri(), t.logo_uri)

    def test_token_trusted_spam_queryset(self):
        spam_tokens = [TokenFactory(spam=True), TokenFactory(spam=True)]
        not_spam_tokens = [
            TokenFactory(spam=False, trusted=False),
            TokenFactory(spam=False, trusted=False),
        ]
        trusted_tokens = [TokenFactory(trusted=True)]

        self.assertCountEqual(Token.objects.spam(), spam_tokens)
        self.assertCountEqual(
            Token.objects.not_spam(), not_spam_tokens + trusted_tokens
        )
        self.assertCountEqual(Token.objects.trusted(), trusted_tokens)

        self.assertIn("SPAM", str(spam_tokens[0]))

    def test_token_create_truncate(self):
        max_length = 60
        long_name = "CHA" + "NA" * 30 + " BATMAN"
        self.assertGreater(len(long_name), max_length)
        truncated_name = long_name[:max_length]
        token = Token.objects.create(
            address=Account.create().address,
            name=long_name,
            symbol=long_name,
            decimals=18,
            trusted=True,
        )
        self.assertEqual(token.name, truncated_name)
        self.assertEqual(token.symbol, truncated_name)

    @mock.patch.object(
        ZerionUniswapV2TokenAdapterClient,
        "get_metadata",
        autospec=True,
        return_value=ZerionPoolMetadata(
            address="0xBA6329EAe69707D6A0F273Bd082f4a0807A6B011",
            name="OWL/USDC Pool",
            symbol="UNI-V2",
            decimals=18,
        ),
    )
    def test_fix_uniswap_pool_tokens(self, get_metadata_mock: MagicMock):
        self.assertEqual(Token.pool_tokens.fix_uniswap_pool_tokens(), 0)
        TokenFactory()
        self.assertEqual(Token.pool_tokens.fix_uniswap_pool_tokens(), 0)
        token = TokenFactory(name="Uniswap V2")
        self.assertEqual(Token.pool_tokens.fix_uniswap_pool_tokens(), 1)
        self.assertEqual(
            Token.pool_tokens.fix_all_pool_tokens(), 0
        )  # Repeating the command will not fix token again
        token.refresh_from_db()
        self.assertEqual(
            token.name, "Uniswap V2 " + get_metadata_mock.return_value.name
        )

    @mock.patch.object(
        BalancerTokenAdapterClient,
        "get_metadata",
        autospec=True,
        return_value=ZerionPoolMetadata(
            address="0x8b6e6E7B5b3801FEd2CaFD4b22b8A16c2F2Db21a",
            name="20% DAI + 80% WETH Pool",
            symbol="BPT",
            decimals=18,
        ),
    )
    def test_fix_balancer_pool_tokens(self, get_metadata_mock: MagicMock):
        self.assertEqual(Token.pool_tokens.fix_balancer_pool_tokens(), 0)
        TokenFactory()
        self.assertEqual(Token.pool_tokens.fix_balancer_pool_tokens(), 0)
        token = TokenFactory(name="Balancer Pool Token")
        self.assertEqual(Token.pool_tokens.fix_balancer_pool_tokens(), 1)
        self.assertEqual(
            Token.pool_tokens.fix_all_pool_tokens(), 0
        )  # Repeating the command will not fix token again
        token.refresh_from_db()
        self.assertEqual(
            token.name, "Balancer Pool Token " + get_metadata_mock.return_value.name
        )
