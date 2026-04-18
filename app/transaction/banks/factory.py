from multiprocessing import Queue, Value


def get_transaction_manager(command: dict, child_status: Queue = None, update_flag: Value = None):
    """
    Factory function — maps an institution name from the server command to
    the correct bank-specific TransactionManager subclass and returns an
    instance of it.

    Called from:
    - main.py run_transaction_manager() — for every TM operation
      (login_check, add_beneficiary, payout, sync, quick_transfer_sbi)

    How it works:
    - Reads institution_name from the command dict (e.g. "KOTAK", "CANARA")
    - Returns the matching TransactionManager subclass instance
    - Returns None if the institution is not recognised — caller raises

    NOTE: driver_manager parameter removed from V1.
    In V2, each TransactionManager creates its own AdsPowerAPI + Browser
    instance internally. No need to pass one from outside.

    V1 location: functions.py get_transaction_manager()
    V2 location: app/transaction/banks/factory.py
    """

    # Imports are inside the function to avoid circular import issues —
    # each bank file imports TransactionManager which imports from utils,
    # so top-level imports here would create a circular dependency
    from app.transaction.banks.kotak import KotakTransactionManager
    from app.transaction.banks.canara import CanaraTransactionManager
    from app.transaction.banks.federal import FederalTransactionManager
    from app.transaction.banks.federal_merchant import FederalMerchantTransactionManager
    from app.transaction.banks.indian_bank import IndianBankTransactionManager
    from app.transaction.banks.karnataka import KarnatakaTransactionManager
    from app.transaction.banks.kvb import KVBTransactionManager
    from app.transaction.banks.rbl import RBLTransactionManager
    from app.transaction.banks.rbl_sp import RBLSPTransactionManager
    from app.transaction.banks.rbl_corporate import RBLCorporateTransactionManager
    from app.transaction.banks.uco import UCOTransactionManager

    institution = command.get('institution_name', '').upper()

    # Map institution name → TransactionManager subclass
    managers = {
        'KOTAK':            KotakTransactionManager,
        'CANARA':           CanaraTransactionManager,
        'FEDERAL':          FederalTransactionManager,
        'FEDERAL_MERCHANT': FederalMerchantTransactionManager,
        'INDIAN_BANK':      IndianBankTransactionManager,
        'KARNATAKA':        KarnatakaTransactionManager,
        'KVB':              KVBTransactionManager,
        'RBL':              RBLTransactionManager,
        'RBL_SP':           RBLSPTransactionManager,
        'RBL_CORPORATE':    RBLCorporateTransactionManager,
        'UCO':              UCOTransactionManager,
    }

    cls = managers.get(institution)
    if not cls:
        return None

    # Instantiate — driver_manager removed, child_status and update_flag kept
    return cls(command, child_status, update_flag)
