"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.
"""


import json
import stat
from json import JSONDecodeError
from typing import Dict
from urllib.parse import urlparse

import os
import rlp
import sys
from abc import ABC, abstractmethod
from cytoolz.dicttoolz import dissoc
from eth_account import Account
from eth_account._utils.transactions import Transaction, assert_valid_fields
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_utils import is_address, to_checksum_address
from eth_utils import to_canonical_address, to_int, apply_formatters_to_dict, apply_key_map
from hexbytes import HexBytes
from trezorlib import ethereum
from trezorlib.client import get_default_client
from trezorlib.tools import parse_path, Address
from web3 import IPCProvider, Web3

from nucypher.blockchain.eth.constants import NULL_ADDRESS
from nucypher.utilities.logging import Logger

try:
    import trezorlib
    from trezorlib.transport import TransportException
    import usb1
except ImportError:
    raise RuntimeError("The nucypher package wasn't installed with the 'trezor' extra.")

from functools import wraps
from typing import Tuple, List

from nucypher.blockchain.eth.decorators import validate_checksum_address


class Signer(ABC):

    URI_SCHEME = NotImplemented
    SIGNERS = NotImplemented  # set dynamically in __init__.py

    log = Logger(__qualname__)

    class SignerError(Exception):
        """Base exception class for signer errors"""

    class InvalidSignerURI(SignerError):
        """Raised when an invalid signer URI is detected"""

    class AccountLocked(SignerError):
        def __init__(self, account: str):
            self.message = f'{account} is locked.'
            super().__init__(self.message)

    class UnknownAccount(SignerError):
        def __init__(self, account: str):
            self.message = f'Unknown account {account}.'
            super().__init__(self.message)

    @classmethod
    def from_signer_uri(cls, uri: str) -> 'Signer':
        parsed = urlparse(uri)
        scheme = parsed.scheme
        signer_class = cls.SIGNERS.get(scheme, Web3Signer)  # Fallback to web3 provider URI for signing
        signer = signer_class.from_signer_uri(uri=uri)
        return signer

    @abstractmethod
    def is_device(self, account: str) -> bool:
        """Some signing client support both software and hardware wallets,
        this method is implemented as a boolean to tell the difference."""
        return NotImplemented

    @property
    @abstractmethod
    def accounts(self) -> List[str]:
        return NotImplemented

    @abstractmethod
    def unlock_account(self, account: str, password: str, duration: int = None) -> bytes:
        return NotImplemented

    @abstractmethod
    def lock_account(self, account: str) -> str:
        return NotImplemented

    @abstractmethod
    def sign_transaction(self, transaction_dict: dict) -> HexBytes:
        return NotImplemented

    @abstractmethod
    def sign_message(self, account: str, message: bytes, **kwargs) -> HexBytes:
        return NotImplemented


class Web3Signer(Signer):

    URI_SCHEME = 'web3'  # TODO: Consider some kind of 'passthough' flag to accept all valid webs provider schemes

    def __init__(self, client):
        super().__init__()
        self.__client = client

    @classmethod
    def from_signer_uri(cls, uri: str) -> 'Web3Signer':
        from nucypher.blockchain.eth.interfaces import BlockchainInterface, BlockchainInterfaceFactory
        try:
            blockchain = BlockchainInterfaceFactory.get_or_create_interface(provider_uri=uri)
        except BlockchainInterface.UnsupportedProvider:
            raise cls.InvalidSignerURI(uri)
        signer = cls(client=blockchain.client)
        return signer

    def is_connected(self) -> bool:
        return self.__client.w3.isConnected()

    @property
    def accounts(self) -> List[str]:
        return self.__client.accounts

    @validate_checksum_address
    def is_device(self, account: str):
        try:
            # TODO: Temporary fix for #1128 and #1385. It's ugly af, but it works. Move somewhere else?
            wallets = self.__client.wallets
        except AttributeError:
            return False
        else:
            HW_WALLET_URL_PREFIXES = ('trezor', 'ledger')
            hw_accounts = [w['accounts'] for w in wallets if w['url'].startswith(HW_WALLET_URL_PREFIXES)]
            hw_addresses = [to_checksum_address(account['address']) for sublist in hw_accounts for account in sublist]
            return account in hw_addresses

    @validate_checksum_address
    def unlock_account(self, account: str, password: str, duration: int = None):
        if self.is_device(account=account):
            unlocked = True
        else:
            unlocked = self.__client.unlock_account(account=account, password=password, duration=duration)
        return unlocked

    @validate_checksum_address
    def lock_account(self, account: str):
        if self.is_device(account=account):
            result = None  # TODO: Force Disconnect Devices?
        else:
            result = self.__client.lock_account(account=account)
        return result

    @validate_checksum_address
    def sign_message(self, account: str, message: bytes, **kwargs) -> HexBytes:
        signature = self.__client.sign_message(account=account, message=message)
        return HexBytes(signature)

    def sign_transaction(self, transaction_dict: dict) -> HexBytes:
        signed_raw_transaction = self.__client.sign_transaction(transaction_dict=transaction_dict)
        return signed_raw_transaction


class ClefSigner(Signer):

    URI_SCHEME = 'clef'

    DEFAULT_IPC_PATH = '~/Library/Signer/clef.ipc' if sys.platform == 'darwin' else '~/.clef/clef.ipc'  #TODO: #1808

    SIGN_DATA_FOR_VALIDATOR = 'data/validator'   # a.k.a. EIP 191 version 0
    SIGN_DATA_FOR_CLIQUE = 'application/clique'  # not relevant for us
    SIGN_DATA_FOR_ECRECOVER = 'text/plain'       # a.k.a. geth's `personal_sign`, EIP-191 version 45 (E)

    DEFAULT_CONTENT_TYPE = SIGN_DATA_FOR_ECRECOVER
    SIGN_DATA_CONTENT_TYPES = (SIGN_DATA_FOR_VALIDATOR, SIGN_DATA_FOR_CLIQUE, SIGN_DATA_FOR_ECRECOVER)

    TIMEOUT = 60  # Default timeout for Clef of 60 seconds

    def __init__(self, ipc_path: str = DEFAULT_IPC_PATH, timeout: int = TIMEOUT):
        super().__init__()
        self.w3 = Web3(provider=IPCProvider(ipc_path=ipc_path, timeout=timeout))  # TODO: Unify with clients or build error handling
        self.ipc_path = ipc_path

    def __ipc_request(self, endpoint: str, *request_args):
        """Error handler for clef IPC requests  # TODO: Use web3 RequestHandler"""
        try:
            response = self.w3.manager.request_blocking(endpoint, request_args)
        except FileNotFoundError:
            raise FileNotFoundError(f'Clef IPC file not found. Is clef running and available at "{self.ipc_path}"?')
        except ConnectionRefusedError:
            raise ConnectionRefusedError(f'Clef refused connection. Is clef running and available at "{self.ipc_path}"?')
        return response

    @classmethod
    def is_valid_clef_uri(cls, uri: str) -> bool:  # TODO: Workaround for #1941
        uri_breakdown = urlparse(uri)
        return uri_breakdown.scheme == cls.URI_SCHEME

    @classmethod
    def from_signer_uri(cls, uri: str) -> 'ClefSigner':
        uri_breakdown = urlparse(uri)
        if not uri_breakdown.path and not uri_breakdown.netloc:
            raise cls.InvalidSignerURI('Blank signer URI - No keystore path provided')
        if uri_breakdown.scheme != cls.URI_SCHEME:
            raise cls.InvalidSignerURI(f"{uri} is not a valid clef signer URI.")
        signer = cls(ipc_path=uri_breakdown.path)
        return signer

    def is_connected(self) -> bool:
        return self.w3.isConnected()

    @validate_checksum_address
    def is_device(self, account: str):
        return True  # TODO: Detect HW v. SW Wallets via clef API - #1772

    @property
    def accounts(self) -> List[str]:
        normalized_addresses = self.__ipc_request(endpoint="account_list")
        checksum_addresses = [to_checksum_address(addr) for addr in normalized_addresses]
        return checksum_addresses

    @validate_checksum_address
    def sign_transaction(self, transaction_dict: dict) -> HexBytes:
        formatters = {
            'nonce': Web3.toHex,
            'gasPrice': Web3.toHex,
            'gas': Web3.toHex,
            'value': Web3.toHex,
            'chainId': Web3.toHex,
            'from': to_checksum_address
        }

        # Workaround for contract creation TXs
        if transaction_dict['to'] == b'':
            transaction_dict['to'] = None
        elif transaction_dict['to']:
            formatters['to'] = to_checksum_address

        formatted_transaction = apply_formatters_to_dict(formatters, transaction_dict)
        signed = self.__ipc_request("account_signTransaction", formatted_transaction)
        return HexBytes(signed.raw)

    @validate_checksum_address
    def sign_message(self, account: str, message: bytes, content_type: str = None, validator_address: str = None, **kwargs) -> HexBytes:
        """
        See https://github.com/ethereum/go-ethereum/blob/a32a2b933ad6793a2fe4172cd46c5c5906da259a/signer/core/signed_data.go#L185
        """
        if isinstance(message, bytes):
            message = Web3.toHex(message)

        if not content_type:
            content_type = self.DEFAULT_CONTENT_TYPE
        elif content_type not in self.SIGN_DATA_CONTENT_TYPES:
            raise ValueError(f'{content_type} is not a valid content type. '
                             f'Valid types are {self.SIGN_DATA_CONTENT_TYPES}')
        if content_type == self.SIGN_DATA_FOR_VALIDATOR:
            if not validator_address or validator_address == NULL_ADDRESS:
                raise ValueError('When using the intended validator type, a validator address is required.')
            data = {'address': validator_address, 'message': message}
        elif content_type == self.SIGN_DATA_FOR_ECRECOVER:
            data = message
        else:
            raise NotImplementedError

        signed_data = self.__ipc_request("account_signData", content_type, account, data)
        return HexBytes(signed_data)

    def sign_data_for_validator(self, account: str, message: bytes, validator_address: str):
        signature = self.sign_message(account=account,
                                      message=message,
                                      content_type=self.SIGN_DATA_FOR_VALIDATOR,
                                      validator_address=validator_address)
        return signature

    @validate_checksum_address
    def unlock_account(self, account: str, password: str, duration: int = None) -> bool:
        return True

    @validate_checksum_address
    def lock_account(self, account: str) -> bool:
        return True


class KeystoreSigner(Signer):
    """Local Web3 signer implementation supporting keystore files"""

    URI_SCHEME = 'keystore'
    __keys: Dict[str, dict]
    __signers: Dict[str, LocalAccount]

    class InvalidKeyfile(Signer.SignerError, RuntimeError):
        """
        Raised when a keyfile is corrupt or otherwise invalid.
        Keystore must be in the geth wallet format.
        """

    def __init__(self, path: str):
        super().__init__()
        self.__path = path
        self.__keys = dict()
        self.__signers = dict()
        self.__read_keystore(path=path)

    def __del__(self):
        # TODO: Might need a finally block or exception context handling
        if self.__keys:
            for account in self.__keys:
                self.lock_account(account)

    def __read_keystore(self, path: str) -> None:
        """Read the keystore directory from the disk and populate accounts."""
        try:
            st_mode = os.stat(path=path).st_mode
            if stat.S_ISDIR(st_mode):
                paths = (entry.path for entry in os.scandir(path=path) if entry.is_file())
            elif stat.S_ISREG(st_mode):
                paths = (path,)
            else:
                raise self.InvalidSignerURI(f'Invalid keystore file or directory "{path}"')
        except FileNotFoundError:
            raise self.InvalidSignerURI(f'No such keystore file or directory "{path}"')
        except OSError as exc:
            raise self.InvalidSignerURI(f'Error accessing keystore file or directory "{path}": {exc}')
        for path in paths:
            account, key_metadata = self.__handle_keyfile(path=path)
            self.__keys[account] = key_metadata

    @staticmethod
    def __read_keyfile(path: str) -> tuple:
        """Read an individual keystore key file from the disk"""
        with open(path, 'r') as keyfile:
            key_metadata = json.load(keyfile)
        address = key_metadata['address']
        return address, key_metadata

    def __handle_keyfile(self, path: str) -> Tuple[str, dict]:
        """
        Read a single keystore file from the disk and return its decoded json contents then internally
        cache it on the keystore instance. Raises InvalidKeyfile if the keyfile is missing or corrupted.
        """
        try:
            address, key_metadata = self.__read_keyfile(path=path)
        except FileNotFoundError:
            error = f"No such keyfile '{path}'"
            raise self.InvalidKeyfile(error)
        except JSONDecodeError:
            error = f"Invalid JSON in keyfile at {path}"
            raise self.InvalidKeyfile(error)
        except KeyError:
            error = f"Keyfile does not contain address field at '{path}'"
            raise self.InvalidKeyfile(error)
        else:
            if not is_address(address):
                raise self.InvalidKeyfile(f"'{path}' does not contain a valid ethereum address")
            address = to_checksum_address(address)
        return address, key_metadata

    @validate_checksum_address
    def __get_signer(self, account: str) -> LocalAccount:
        """Lookup a known keystore account by its checksum address or raise an error"""
        try:
            return self.__signers[account]
        except KeyError:
            if account not in self.__keys:
                raise self.UnknownAccount(account=account)
            else:
                raise self.AccountLocked(account=account)

    #
    # Public API
    #

    @property
    def path(self) -> str:
        """Read only access to the keystore path"""
        return self.__path

    @classmethod
    def from_signer_uri(cls, uri: str) -> 'Signer':
        """Return a keystore signer from URI string i.e. keystore:///my/path/keystore """
        decoded_uri = urlparse(uri)
        if decoded_uri.scheme != cls.URI_SCHEME or decoded_uri.netloc:
            raise cls.InvalidSignerURI(uri)
        return cls(path=decoded_uri.path)

    @validate_checksum_address
    def is_device(self, account: str) -> bool:
        return False  # Keystore accounts are never devices.

    @property
    def accounts(self) -> List[str]:
        """Return a list of known keystore accounts read from"""
        return list(self.__keys.keys())

    @validate_checksum_address
    def unlock_account(self, account: str, password: str, duration: int = None) -> bool:
        """
        Decrypt the signing material from the key metadata file and cache it on
        the keystore instance is decryption is successful.
        """
        if not self.__signers.get(account):
            try:
                key_metadata = self.__keys[account]
            except ValueError:
                return False  # Decryption Failed
            except KeyError:
                raise self.UnknownAccount(account=account)
            else:
                # TODO: It is possible that password is None here passed form the above leayer,
                #       causing Account.decrypt to crash, expecting a value for password.
                signing_key = Account.from_key(Account.decrypt(key_metadata, password))
                self.__signers[account] = signing_key
        return True

    @validate_checksum_address
    def lock_account(self, account: str) -> bool:
        """
        Deletes a local signer by its checksum address or raises UnknownAccount if
        the address is not a member of this keystore.  Returns True if the account is no longer
        tracked and was successfully locked.
        """
        try:
            self.__signers.pop(account)  # mutate
        except KeyError:
            if account not in self.accounts:
                raise self.UnknownAccount(account=account)
        return account not in self.__signers

    @validate_checksum_address
    def sign_transaction(self, transaction_dict: dict) -> HexBytes:
        """
        Produce a raw signed ethereum transaction signed by the account specified
        in the 'from' field of the transaction dictionary.
        """

        sender = transaction_dict['from']
        signer = self.__get_signer(account=sender)

        # TODO: Handle this at a higher level?
        # Do not include a 'to' field for contract creation.
        if not transaction_dict['to']:
            transaction_dict = dissoc(transaction_dict, 'to')

        raw_transaction = signer.sign_transaction(transaction_dict=transaction_dict).rawTransaction
        return raw_transaction

    @validate_checksum_address
    def sign_message(self, account: str, message: bytes, **kwargs) -> HexBytes:
        signer = self.__get_signer(account=account)
        signature = signer.sign_message(signable_message=encode_defunct(primitive=message)).signature
        return signature


class TrezorSigner(Signer):
    """
    An implementation of a Trezor device for staking on the NuCypher network.
    """

    URI_SCHEME = 'trezor'
    ADDRESS_CACHE_SIZE = 3
    ETH_CHAIN_ROOT = 60

    class NoDeviceDetected(RuntimeError):
        pass

    def __init__(self):
        try:
            self.client = get_default_client()
        except TransportException:
            raise self.NoDeviceDetected("Could not find a TREZOR device to connect to. Have you unlocked it?")

        self._device_id = self.client.get_device_id()
        self.__addresses = dict()
        self.__load_addresses()

    @classmethod
    def from_signer_uri(cls, uri: str) -> 'TrezorSigner':
        """Return a trezor signer from URI string i.e. trezor:///my/trezor/path """
        decoded_uri = urlparse(uri)
        if decoded_uri.scheme != cls.URI_SCHEME or decoded_uri.netloc:
            raise cls.InvalidSignerURI(uri)
        return cls()

    def is_device(self, account: str) -> bool:
        return True

    @validate_checksum_address
    def unlock_account(self, account: str, password: str, duration: int = None) -> bool:
        return True

    @validate_checksum_address
    def lock_account(self, account: str) -> bool:
        return True

    def get_address_path(self, index: int = None, checksum_address: str = None) -> List[int]:
        if index is not None and checksum_address:
            raise ValueError("Expected index or checksum address; Got both.")
        elif index is not None:
            hd_path = parse_path(f"44'/{self.ETH_CHAIN_ROOT}'/0'/0/{index}")  # TODO: cleanup
        else:
            try:
                hd_path = self.__addresses[checksum_address]
            except KeyError:
                raise RuntimeError(f"{checksum_address} was not loaded into the device address cache.")
        return hd_path

    def __load_addresses(self):
        for index in range(self.ADDRESS_CACHE_SIZE):
            hd_path = self.get_address_path(index=index)
            address = self.get_address(hd_path=hd_path, show_display=False)
            self.__addresses[address] = hd_path

    @property
    def accounts(self) -> List[str]:
        return list(self.__addresses.keys())

    #
    # Device Calls
    #

    def __handle_device_call(device_func):
        @wraps(device_func)
        def wrapped_call(trezor, *args, **kwargs):
            try:
                result = device_func(trezor, *args, **kwargs)
            except usb1.USBErrorNoDevice:
                error = "The client cannot communicate to the TREZOR USB device. Was it disconnected?"
                raise trezor.NoDeviceDetected(error)
            except usb1.USBErrorBusy:
                raise trezor.DeviceError("The TREZOR USB device is busy.")
            else:
                return result
        return wrapped_call

    @__handle_device_call
    def get_address(self, index: int = None, hd_path: Address = None, show_display: bool = True) -> str:
        if not hd_path:
            if index is None:
                raise ValueError("No index or HD path supplied.")  # TODO: better error handling here
            hd_path = self.get_address_path(index=index)
        address = ethereum.get_address(client=self.client, n=hd_path, show_display=show_display)
        return address

    @__handle_device_call
    def sign_message(self, message: bytes, checksum_address: str):
        """
        Signs a message via the TREZOR ethereum sign_message API and returns
        the signature and the address used to sign it. This method requires
        interaction between the TREZOR and the user.

        If an address_index is provided, it will use the address at that
        index to sign the message. If no index is provided, the address at
        the 0th index is used by default.
        """
        hd_path = self.get_address_path(checksum_address=checksum_address)
        sig = trezorlib.ethereum.sign_message(self.client, hd_path, message)
        return self.Signature(sig.signature, sig.address)

    @__handle_device_call
    def sign_transaction(self,
                         transaction_dict: dict,
                         rlp_encoded: bool = True,
                         ) -> Tuple[bytes]:

        # Read the sender inside the transaction request
        checksum_address = transaction_dict.pop('from')

        # Handle Web3.py -> Trezor native transaction formatting
        # https://web3py.readthedocs.io/en/latest/web3.eth.html#web3.eth.Eth.sendRawTransaction
        assert_valid_fields(transaction_dict)
        trezor_transaction_keys = {'gas': 'gas_limit', 'gasPrice': 'gas_price', 'chainId': 'chain_id'}
        trezor_transaction = dict(apply_key_map(trezor_transaction_keys, transaction_dict))

        # Format data
        if trezor_transaction.get('data'):
            trezor_transaction['data'] = Web3.toBytes(HexBytes(trezor_transaction['data']))
            transaction_dict['data'] = Web3.toBytes(HexBytes(transaction_dict['data']))

        # Lookup HD path & Sign Transaction
        n = self.get_address_path(checksum_address=checksum_address)

        # Sign TX
        v, r, s = trezorlib.ethereum.sign_tx(client=self.client, n=n, **trezor_transaction)

        # If `chain_id` is included, an EIP-155 transaction signature will be applied:
        # v = (v + 2) * (chain_id + 35)
        # https://github.com/ethereum/eips/issues/155
        # https://github.com/trezor/trezor-core/pull/311
        del transaction_dict['chainId']   # see above

        # Create RLP serializable Transaction
        transaction_dict['to'] = to_canonical_address(checksum_address)
        signed_transaction = Transaction(v=to_int(v),
                                         r=to_int(r),
                                         s=to_int(s),
                                         **transaction_dict)
        if rlp_encoded:
            signed_transaction = rlp.encode(signed_transaction)

        return signed_transaction
