from . import DATA_DIR
from .clean_datasets import DatabaseCleaner
from .data_collection import RemindDataCollection
from .electricity import Electricity
from .inventory_imports import CarmaCCSInventory
import pyprind
import wurst


FILEPATH_CARMA_INVENTORIES = (DATA_DIR / "lci-Carma-CCS.xlsx")
FILEPATH_BIO_INVENTORIES = (DATA_DIR / "bioenergy_cozzolini_2018.csv")


class NewDatabase:
    """
    Class that represents a new wurst inventory database, modified according to IAM data.

    :ivar database_dict: dictionary with scenarios to create
    :vartype database_dict: dict
    :ivar destination_db: the ecoinvent source database
    :vartype destination_db: str
    :ivar destination_version: the version of the ecoinvent source database
    :vartype destination_db: float
    :ivar filepath_to_remind_files: Filepath to the directory that contains REMIND output files.
    :vartype filepath_to_remind_file: pathlib.Path

    """

    def __init__(self, database_dict, destination_db, destination_version=3.5, filepath_to_remind_files=None):
        self.scenarios = database_dict
        self.destination = destination_db
        self.version = destination_version
        self.db = self.clean_database()
        self.import_inventories()
        self.filepath_to_remind_files = (filepath_to_remind_files or DATA_DIR / "Remind output files")

    def clean_database(self):
        return DatabaseCleaner(self.destination).prepare_datasets()

    def import_inventories(self):
        # Add Carma CCS inventories
        print("Add Carma CCS inventories")
        carma = CarmaCCSInventory(self, FILEPATH_CARMA_INVENTORIES)
        carma.merge_inventory()

    def update_electricity_to_remind_data(self):
        for s in pyprind.prog_bar(self.scenarios.items()):
            scenario, year = s
            rdc = RemindDataCollection(scenario, year, self.filepath_to_remind_files)
            El = Electricity(self.db, rdc, scenario, year)
            self.db = El.update_electricity_markets()
            self.db = El.update_electricity_efficiency()

    def write_db_to_brightway(self):
        for s in pyprind.prog_bar(self.scenarios.items()):
            scenario, year = s

            print('Write new database to Brightway2.')
            wurst.write_brightway2_database(self.db, "ecoinvent_"+ scenario + "_" + str(year))
