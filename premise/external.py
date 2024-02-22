"""
Implements external scenario data.
"""

import logging
import uuid
from collections import defaultdict
from pathlib import Path
from typing import List, Union

import numpy as np
import wurst
import xarray as xr
import yaml
from datapackage import Package
from wurst import searching as ws

from .clean_datasets import get_biosphere_flow_uuid
from .data_collection import IAMDataCollection
from .external_data_validation import check_inventories, find_iam_efficiency_change
from .filesystem_constants import DATA_DIR
from .inventory_imports import (
    AdditionalInventory,
    generate_migration_maps,
    get_biosphere_code,
    get_correspondence_bio_flows,
)
from .transformation import BaseTransformation, get_shares_from_production_volume
from .utils import HiddenPrints, rescale_exchanges

LOG_CONFIG = DATA_DIR / "utils" / "logging" / "logconfig.yaml"

# directory for log files
DIR_LOGS = Path.cwd() / "export" / "logs"
# if DIR_LOG_REPORT folder does not exist
# we create it
if not Path(DIR_LOGS).exists():
    Path(DIR_LOGS).mkdir(parents=True, exist_ok=True)

with open(LOG_CONFIG, encoding="utf-8") as f:
    config = yaml.safe_load(f.read())
    logging.config.dictConfig(config)

logger = logging.getLogger("external")


def _update_external_scenarios(
    scenario: dict,
    version: str,
    system_model: str,
    datapackages: list,
) -> dict:
    datapackages = [
        Package(f"{dp}/datapackage.json") if isinstance(dp, str) else dp
        for dp in datapackages
    ]
    for d, data_package in enumerate(datapackages):
        inventories = []
        with HiddenPrints():
            if "inventories" in [r.name for r in data_package.resources]:
                if data_package.get_resource("inventories"):
                    additional = AdditionalInventory(
                        database=scenario["database"],
                        version_in=data_package.descriptor["ecoinvent"]["version"],
                        version_out=version,
                        path=data_package.get_resource("inventories").source,
                        system_model=system_model,
                    )
                    inventories.extend(additional.merge_inventory())

        resource = data_package.get_resource("config")
        config_file = yaml.safe_load(resource.raw_read())

        checked_inventories, checked_database = check_inventories(
            configuration=config_file,
            inventory_data=inventories,
            scenario_data=scenario["external data"][d],
            database=scenario["database"],
            year=scenario["year"],
            model=scenario["model"],
        )

        scenario["database"] = checked_database
        scenario["database"].extend(checked_inventories)

    external_scenario = ExternalScenario(
        database=scenario["database"],
        model=scenario["model"],
        pathway=scenario["pathway"],
        iam_data=scenario["iam data"],
        year=scenario["year"],
        external_scenarios=datapackages,
        external_scenarios_data=scenario["external data"],
        version=version,
        system_model=system_model,
    )
    external_scenario.create_markets()
    external_scenario.relink_datasets()
    scenario["database"] = external_scenario.database
    return scenario


def get_mapping_between_ei_versions(version_in: str, version_out: str) -> dict:
    mapping = generate_migration_maps(
        version_in.replace(".", ""), version_out.replace(".", "")
    )["data"]
    m = {}

    for i, j in mapping:
        m[(i[0], i[1])] = j

    return m


def fetch_loc(loc: str) -> Union[str, None]:
    if isinstance(loc, str):
        return loc
    if isinstance(loc, tuple):
        if loc[0] == "ecoinvent":
            return loc[1]
    return None


def get_recursively(search_dict: dict, field: str) -> list:
    """Takes a dict with nested lists and dicts,
    and searches all dicts for a key of the field
    provided.
    :param search_dict: dict with nested lists and dicts
    :param field: field to search for
    :return: list of values for the field
    """
    fields_found = []

    for key, value in search_dict.items():
        if key == field:
            fields_found.append(value)

        elif isinstance(value, dict):
            results = get_recursively(value, field)
            for result in results:
                fields_found.append(result)

        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    more_results = get_recursively(item, field)
                    for another_result in more_results:
                        fields_found.append(another_result)

    return fields_found


def adjust_efficiency(dataset: dict) -> dict:
    """
    Adjust the input-to-output efficiency of a dataset and return it back.
    :param dataset: dataset to be adjusted
    :return: adjusted dataset
    """

    # loop through the type of flows to adjust
    filters = []
    for eff_type in ["technosphere", "biosphere"]:
        if f"{eff_type} filters" in dataset:
            for v in dataset[f"{eff_type} filters"].values():
                if dataset["location"] not in v[1]:
                    continue

                # the scaling factor is the inverse of the efficiency change
                if len(dataset["regions"]) > 1:
                    try:
                        scaling_factor = 1 / v[1][dataset["location"]]
                    except KeyError:
                        print(
                            f"No efficiency factor provided for dataset {dataset['name']} in {dataset['location']}"
                        )
                        scaling_factor = 1
                else:
                    try:
                        scaling_factor = 1 / v[1].get(dataset["regions"][0], 1)
                    except ZeroDivisionError:
                        scaling_factor = 1

                filters.append(ws.either(*[ws.contains("name", x) for x in v[0]]))

                # check if "excludes" is in the filters
                if f"excludes {eff_type}" in dataset:
                    if v[1] in dataset[f"excludes {eff_type}"]:
                        filters.append(ws.doesnt_contain_any(
                            "name", dataset[f"excludes {eff_type}"][v[1]]
                        ))

                if not np.isclose(scaling_factor, 1, rtol=1e-3):
                    if "log parameters" not in dataset:
                        dataset["log parameters"] = {}

                    if eff_type == "technosphere":
                        # adjust technosphere flows
                        # all of them if no filters are provided
                        dataset["log parameters"][
                            "technosphere scaling factor"
                        ] = scaling_factor
                        if filters:
                            for exc in ws.technosphere(
                                dataset,
                                *filters,
                            ):
                                wurst.rescale_exchange(
                                    exc, scaling_factor, remove_uncertainty=False
                                )
                        else:
                            for exc in ws.technosphere(
                                dataset,
                            ):
                                wurst.rescale_exchange(
                                    exc, scaling_factor, remove_uncertainty=False
                                )
                    else:
                        # adjust biosphere flows
                        # all of them if a filter is not provided
                        dataset["log parameters"][
                            "biosphere scaling factor"
                        ] = scaling_factor

                        if filters:
                            for exc in ws.biosphere(
                                dataset,
                              *filters,
                            ):
                                wurst.rescale_exchange(
                                    exc, scaling_factor, remove_uncertainty=False
                                )
                        else:
                            for exc in ws.biosphere(
                                dataset,
                            ):
                                wurst.rescale_exchange(
                                    exc, scaling_factor, remove_uncertainty=False
                                )
    return dataset


def fetch_dataset_description_from_production_pathways(
    configuration: dict, item: str
) -> Union[tuple, None]:
    """
    Fetch a few ecoinvent variables for a given production pathway
    in the config file, such as the name, reference product, etc.
    :param configuration: config file
    :param item: production pathway
    :return: dictionary with variables
    """
    for p, v in configuration["production pathways"].items():
        if p == item:
            if "exists in original database" not in v["ecoinvent alias"]:
                v["ecoinvent alias"].update({"exists in original database": True})

            if "new dataset" not in v["ecoinvent alias"]:
                v["ecoinvent alias"].update({"new dataset": False})

            if "regionalize" not in v["ecoinvent alias"]:
                v["ecoinvent alias"].update({"regionalize": False})

            return (
                v["ecoinvent alias"]["name"],
                v["ecoinvent alias"]["reference product"],
                v["ecoinvent alias"]["exists in original database"],
                v["ecoinvent alias"]["new dataset"],
                v["ecoinvent alias"]["regionalize"],
                v["ecoinvent alias"].get("ratio", 1),
            )
    return


def fetch_var(config_file: dict, list_vars: list) -> list:
    """
    Return the value for `variable` for the given list of variables.
    :param config_file: config file
    :param list_vars: list of variables
    :return: value for `variable`
    """

    return [
        config_file["production pathways"][v]["production volume"]["variable"]
        for v in list_vars
    ]


class ExternalScenario(BaseTransformation):
    def __init__(
        self,
        database: List[dict],
        iam_data: IAMDataCollection,
        external_scenarios: list,
        external_scenarios_data: dict,
        model: str,
        pathway: str,
        year: int,
        version: str,
        system_model: str,
    ):
        """
        :param database: list of datasets representing teh database
        :param iam_data: IAM data: production volumes, efficiency, etc.
        :param external_scenarios: list of data packages representing the external scenarios
        :param external_scenarios_data: IAM data: production volumes, efficiency, etc.
        :param model: model name
        :param pathway: pathway name
        :param year: year

        """
        super().__init__(
            database,
            iam_data,
            model,
            pathway,
            year,
            version,
            system_model,
        )
        self.datapackages = external_scenarios
        self.external_scenarios_data = external_scenarios_data
        self.biosphere_flows = get_biosphere_code(self.version)

        for datapackage_number, datapackage in enumerate(self.datapackages):
            external_scenario_regions = self.external_scenarios_data[
                datapackage_number
            ]["regions"]
            # Open corresponding config file
            resource = datapackage.get_resource("config")
            config_file = yaml.safe_load(resource.raw_read())
            ds_names = get_recursively(config_file, "name")
            self.regionalize_inventories(
                ds_names, external_scenario_regions, datapackage_number
            )
        self.dict_bio_flows = get_biosphere_flow_uuid(self.version)
        self.outdated_flows = get_correspondence_bio_flows()

    def regionalize_inventories(
        self, ds_names, regions, datapackage_number: int
    ) -> None:
        """
        Produce IAM region-specific version of the dataset.
        :param regions: list of regions to produce datasets for

        """

        for ds in ws.get_many(
            self.database,
            ws.equals("regionalize", True),
            ws.either(*[ws.contains("name", name) for name in ds_names]),
        ):

            # Check if datasets already exist for IAM regions
            # if not, create them
            if ds["location"] not in regions:
                new_acts = self.fetch_proxies(
                    name=ds["name"],
                    ref_prod=ds["reference product"],
                    regions=ds["regions"],
                )

                # add production volume
                if ds.get("production volume variable"):
                    if (
                        ds["production volume variable"]
                        in self.external_scenarios_data[datapackage_number][
                            "production volume"
                        ].variables.values
                    ):
                        for region, act in new_acts.items():
                            if (
                                region
                                in self.external_scenarios_data[datapackage_number][
                                    "production volume"
                                ].region.values
                            ):
                                act["production volume"] = (
                                    self.external_scenarios_data[datapackage_number][
                                        "production volume"
                                    ]
                                    .sel(
                                        region=region,
                                        variables=ds["production volume variable"],
                                    )
                                    .interp(year=self.year)
                                    .values
                                )

                # add new datasets to database
                self.database.extend(new_acts.values())

                for _, act in new_acts.items():
                    # add to log
                    self.write_log(act)
                    self.add_to_index(act)

            # remove "regionalize" tag
            if "regionalize" in ds:
                del ds["regionalize"]

        # some datasets might be meant to replace the supply
        # of other datasets, so we need to adjust those
        replacing_acts = {
            (ds["name"], ds["reference product"]): {
                "replaces": ds["replaces"],
                "replaces in": ds.get("replaces in", None),
                "replacement ratio": ds.get("replacement ratio", 1),
                "regions": ds.get("regions", regions),
            }
            for ds in self.database
            if "replaces" in ds and ds["name"] in ds_names
        }

        for (name, ref_prod), values in replacing_acts.items():
            self.relink_to_new_datasets(
                replaces=values["replaces"],
                replaces_in=values.get("replaces in", None),
                new_name=name,
                new_ref=ref_prod,
                ratio=values.get("replacement ratio", 1),
                regions=values.get("regions", regions),
                isfuel=values.get("is fuel"),
            )

        # adjust efficiency of datasets
        for dataset in ws.get_many(
            self.database,
            ws.equals("adjust efficiency", True),
            ws.either(*[ws.contains("name", name) for name in ds_names]),
        ):
            if len(dataset["location"]) > 1:
                adjust_efficiency(dataset)
                if dataset.get("log parameters", {}).get(
                    "technosphere scaling factor"
                ) or dataset.get("log parameters", {}).get("biosphere scaling factor"):
                    self.write_log(dataset, status="updated")
            del dataset["adjust efficiency"]

    def get_market_dictionary_structure(
        self, market: dict, region: str, waste_market: bool = False
    ) -> dict:
        """
        Return a dictionary for market creation, given the location passed.
        To be further filled with exchanges.
        :param market: dataset to use as template
        :param region: region to create the dataset for.
        :param waste_market: True if the market is a waste market
        :return: dictionary
        """

        return {
            "name": market["name"],
            "reference product": market["reference product"],
            "unit": market["unit"],
            "location": region,
            "database": "premise",
            "code": str(uuid.uuid4().hex),
            "exchanges": [
                {
                    "name": market["name"],
                    "product": market["reference product"],
                    "unit": market["unit"],
                    "location": region,
                    "type": "production",
                    "amount": -1 if waste_market else 1,
                }
            ],
        }

    def fill_in_world_market(
        self,
        market: dict,
        regions: list,
        i: int,
        pathways: list,
        waste_market: bool = False,
    ) -> dict:
        """
        Fill in the world market with the supply of all regional markets
        based on their respective production volumes.
        :param market: World market dataset
        :param regions: List of regions
        :param i: index of production volumes array in external_data
        :param pathways: List of production pathways
        :param waste_market: True if the market is a waste market
        :return: World market dataset
        """

        # fetch a template for the world market dataset
        world_market = self.get_market_dictionary_structure(
            market=market, region="World", waste_market=waste_market
        )
        new_excs = []

        word_production_volume = (
            self.external_scenarios_data[i]["production volume"]
            .sel(variables=pathways, region=[r for r in regions if r != "World"])
            .sum(dim=["variables", "region"])
            .interp(year=self.year)
            .values.item(0)
        )

        # update production volume field in the world market
        for e in ws.production(world_market):
            e["production volume"] = word_production_volume

        # fetch the supply share for each regional market
        for region in regions:
            supply_share = np.clip(
                (
                    self.external_scenarios_data[i]["production volume"]
                    .sel(region=region, variables=pathways)
                    .sum(dim="variables")
                    .interp(year=self.year)
                    / self.external_scenarios_data[i]["production volume"]
                    .sel(
                        variables=pathways, region=[r for r in regions if r != "World"]
                    )
                    .sum(dim=["variables", "region"])
                    .interp(year=self.year)
                ).values.item(0),
                0,
                1,
            )

            if supply_share == 0:
                continue
            # create a new exchange for the regional market
            # in the World market dataset
            new_excs.append(
                {
                    "name": market["name"],
                    "product": market["reference product"],
                    "unit": market["unit"],
                    "location": region,
                    "type": "technosphere",
                    "amount": supply_share * -1 if waste_market else supply_share,
                }
            )

        world_market["exchanges"].extend(new_excs)

        return world_market

    def check_existence_of_market_suppliers(self):
        """
        Check if the market suppliers are in the database.

        """

        # Loop through custom scenarios
        for i, dp in enumerate(self.datapackages):
            # Open corresponding config file
            resource = dp.get_resource("config")
            config_file = yaml.safe_load(resource.raw_read())

            # Check if information on market creation is provided
            if "markets" in config_file:
                for market in config_file["markets"]:
                    # Loop through the technologies that should compose the market
                    for pathway_to_include in market["includes"]:
                        # fetch the dataset name/ref corresponding to this item
                        # under `production pathways`
                        (
                            name,
                            ref_prod,
                            exists_in_database,
                            new_dataset,
                            regionalize_dataset,
                            _,
                        ) = fetch_dataset_description_from_production_pathways(
                            config_file, pathway_to_include
                        )

                        if not new_dataset:
                            regions = self.external_scenarios_data[i]["regions"]

                            # try to see if we find a provider for these regions
                            if any(region in self.regions for region in regions):
                                ecoinvent_regions = [
                                    self.geo.iam_to_ecoinvent_location(region)
                                    for region in regions
                                ]
                            else:
                                ecoinvent_regions = [
                                    fetch_loc(r)
                                    for r in [
                                        y
                                        for x in regions
                                        for y in self.geo.geo.within(x)
                                    ]
                                ]

                                ecoinvent_regions = [
                                    i
                                    for i in ecoinvent_regions
                                    if i and i not in ["GLO", "RoW"]
                                ]

                                if len(ecoinvent_regions) == 0:
                                    ecoinvent_regions = [
                                        i
                                        for i in list(self.geo.geo.keys())
                                        if isinstance(i, str) and i != "GLO"
                                    ]

                            possible_locations = [
                                *regions,
                                *ecoinvent_regions,
                                "RoW",
                                "GLO",
                                "RER",
                                "Europe without Switzerland",
                                # add all ecoinvent locations
                                *[fetch_loc(loc) for loc in list(self.geo.geo.keys())],
                            ]

                            suppliers, counter = [], 0

                            # we loop through the possible locations
                            # by order of preference
                            try:
                                while not suppliers:
                                    suppliers = list(
                                        ws.get_many(
                                            self.database,
                                            ws.equals("name", name),
                                            ws.equals(
                                                "reference product",
                                                ref_prod,
                                            ),
                                            ws.equals(
                                                "location", possible_locations[counter]
                                            ),
                                        )
                                    )

                                    counter += 1

                            except IndexError as err:
                                raise ValueError(
                                    f"Regionalized datasets for pathway {pathway_to_include} "
                                    f"with `name` {name} and `reference product` {ref_prod} "
                                    f"cannot be found in "
                                    f"locations {possible_locations}."
                                ) from err

                            if not exists_in_database or regionalize_dataset:
                                for ds in suppliers:
                                    ds["custom scenario dataset"] = True

    def fetch_supply_share(
        self, i: int, region: str, var: str, variables: list
    ) -> np.ndarray:
        """
        Return the supply share of a given variable in a given region.
        :param i: index of the scenario
        :param region: region
        :param var: variable
        :param variables: list of all variables
        :return: np.ndarray
        """

        return np.clip(
            (
                self.external_scenarios_data[i]["production volume"]
                .sel(
                    region=region,
                    variables=var,
                )
                .interp(year=self.year)
                / self.external_scenarios_data[i]["production volume"]
                .sel(
                    region=region,
                    variables=variables,
                )
                .interp(year=self.year)
                .sum(dim="variables")
            ).values.item(0),
            0,
            1,
        )

    def fetch_potential_suppliers(
        self, possible_locations: list, name: str, ref_prod: str
    ) -> list:
        """
        Fetch the potential suppliers for a given name and reference product.
        :param possible_locations: list of possible locations
        :param name: name of the dataset
        :param ref_prod: reference product of the dataset
        :return: list of potential suppliers
        """

        act, counter = [], 0
        try:
            while not act:
                act = list(
                    ws.get_many(
                        self.database,
                        ws.equals("name", name),
                        ws.equals(
                            "reference product",
                            ref_prod,
                        ),
                        ws.equals("location", possible_locations[counter]),
                    )
                )

                counter += 1
        except IndexError:
            print("Cannot find -> ", name, ref_prod, possible_locations)

        return act

    def write_suppliers_exchanges(self, suppliers: dict, supply_share: float) -> list:
        """
        Write the exchanges for the suppliers.
        :param suppliers: list of suppliers
        :param supply_share: supply share
        :return: list of exchanges
        """

        new_excs = []

        for supplier, market_share in suppliers.items():
            provider_share = supply_share * market_share

            new_excs.append(
                {
                    "name": supplier[0],
                    "product": supplier[2],
                    "unit": supplier[-1],
                    "location": supplier[1],
                    "type": "technosphere",
                    "amount": provider_share,
                    "uncertainty type": 0,
                }
            )

        return new_excs

    def add_additional_exchanges(
        self, additional_exc: dict, region: str, ei_version: str
    ) -> list:
        """
        Add additional exchanges to a dataset.
        """

        name = additional_exc["name"]
        ref_prod = additional_exc.get("reference product")
        categories = additional_exc.get("categories")
        unit = additional_exc.get("unit")
        amount = additional_exc["amount"]

        # we need to ensure that the dataset exists
        # to do so, we need to load migration.csv
        # and check if the dataset is there
        # if it is there, we need to use instead the new values

        mapping = get_mapping_between_ei_versions(str(ei_version), self.version)

        if (name, ref_prod) in mapping:
            name = mapping[(name, ref_prod)]["name"]
            ref_prod = mapping[(name, ref_prod)]["reference product"]

        if ref_prod:
            # this is a technosphere exchange
            if region in self.geo.iam_regions:
                ecoinvent_regions = self.geo.iam_to_ecoinvent_location(region)
            else:
                ecoinvent_regions = [fetch_loc(r) for r in self.geo.geo.within(region)]
                ecoinvent_regions = [
                    i for i in ecoinvent_regions if i and i not in ["GLO", "RoW"]
                ]

            possible_locations = [
                region,
                *ecoinvent_regions,
                "RoW",
                "GLO",
                "RER",
                "Europe without Switzerland",
                # add all ecoinvent locations
                *[fetch_loc(loc) for loc in list(self.geo.geo.keys())],
            ]
            potential_suppliers = self.fetch_potential_suppliers(
                possible_locations, name, ref_prod
            )
            suppliers = get_shares_from_production_volume(potential_suppliers)

            return self.write_suppliers_exchanges(suppliers, amount)

        # this is a biosphere exchange
        categories = tuple(categories.split("::"))
        if len(categories) == 1:
            key = (name, categories[0], "unspecified", unit)
        else:
            key = (name, categories[0], categories[1], unit)

        if key not in self.dict_bio_flows:
            if key[0] in self.outdated_flows:
                key = (self.outdated_flows[key[0]], key[1], key[2], key[3])
            else:
                raise ValueError(
                    f"Cannot find biosphere flow {key} in the biosphere database."
                )

        return [
            {
                "name": name,
                "unit": unit,
                "categories": categories,
                "type": "biosphere",
                "amount": amount,
                "uncertainty type": 0,
                "input": (
                    "biosphere3",
                    self.dict_bio_flows[key],
                ),
            }
        ]

    def adjust_efficiency_of_new_markets(
        self, datatset: dict, variables: dict, region: str, eff_data: xr.DataArray
    ) -> dict:
        for ineff in variables["efficiency"]:
            scaling_factor = 1 / find_iam_efficiency_change(
                ineff["variable"], region, eff_data, self.year
            )

            if "includes" not in ineff:
                rescale_exchanges(datatset, scaling_factor, remove_uncertainty=False)

            else:
                if "technosphere" in ineff["includes"]:
                    fltr = []
                    for y in ineff["includes"]["technosphere"]:
                        for k, v in y.items():
                            fltr.append(wurst.contains(k, v))

                    for exc in ws.technosphere(datatset, *(fltr or [])):
                        wurst.rescale_exchange(
                            exc, scaling_factor, remove_uncertainty=False
                        )

                if "biosphere" in ineff["includes"]:
                    fltr = []
                    for y in ineff["includes"]["biosphere"]:
                        for k, v in y.items():
                            fltr.append(wurst.contains(k, v))

                    for exc in ws.biosphere(datatset, *(fltr or [])):
                        wurst.rescale_exchange(
                            exc, scaling_factor, remove_uncertainty=False
                        )
        return datatset

    def get_region_for_non_null_production_volume(self, i, variables):
        nz = np.argwhere(
            (
                self.external_scenarios_data[i]["production volume"]
                .sel(variables=variables)
                .sum(dim=["year", "variables"])
                > 0
            ).values
        )

        return [
            self.external_scenarios_data[i]["production volume"]
            .coords["region"][x[0]]
            .values.item(0)
            for x in nz
        ]

    def create_markets(self) -> None:
        """
        Create new markets, and create a `World` market
        if no data is provided for it.

        """

        self.check_existence_of_market_suppliers()

        # Loop through custom scenarios
        for i, dp in enumerate(self.datapackages):
            # Open corresponding config file
            resource = dp.get_resource("config")
            config_file = yaml.safe_load(resource.raw_read())

            # Check if information on market creation is provided
            if "markets" in config_file:
                for market_vars in config_file["markets"]:
                    # fetch all scenario file variables that
                    # relate to this market
                    pathways = market_vars["includes"]
                    production_variables = fetch_var(config_file, pathways)
                    waste_market = market_vars.get("waste market", False)
                    isfuel = {}
                    market_status = {}

                    # Check if there are regions we should not
                    # create a market for
                    regions = self.get_region_for_non_null_production_volume(
                        i=i, variables=production_variables
                    )

                    if "except regions" in market_vars:
                        regions = [
                            r for r in regions if r not in market_vars["except regions"]
                        ]

                    # Loop through regions
                    for region in regions:
                        # Create market dictionary
                        new_market = self.get_market_dictionary_structure(
                            market=market_vars, region=region, waste_market=waste_market
                        )

                        production_volume = (
                            self.external_scenarios_data[i]["production volume"]
                            .sel(variables=production_variables, region=region)
                            .sum(dim="variables")
                            .interp(year=self.year)
                            .values.item(0)
                        )

                        # Update production volume of the market
                        for e in ws.production(new_market):
                            e["production volume"] = production_volume

                        new_excs = []
                        for pathway in pathways:
                            var = fetch_var(config_file, [pathway])[0]

                            # fetch the dataset name/ref corresponding to this item
                            # under `production pathways`
                            (name, ref_prod, _, _, _, ratio) = (
                                fetch_dataset_description_from_production_pathways(
                                    config_file, pathway
                                )
                            )

                            # try to see if we find a
                            # provider with that region
                            if region in self.regions:
                                ecoinvent_regions = []

                            else:
                                ecoinvent_regions = [
                                    i
                                    for i in [
                                        fetch_loc(r)
                                        for r in self.geo.geo.within(region)
                                    ]
                                    if i and i not in ["GLO", "RoW"]
                                ]

                            possible_locations = [
                                region,
                                *ecoinvent_regions,
                                "RoW",
                                "GLO",
                                "RER",
                                "Europe without Switzerland",
                                # add all ecoinvent locations
                                *[fetch_loc(loc) for loc in list(self.geo.geo.keys())],
                            ]

                            potential_suppliers = self.fetch_potential_suppliers(
                                possible_locations, name, ref_prod
                            )

                            # supply share = production volume of that technology in this region
                            # over production volume of all technologies in this region

                            try:
                                supply_share = self.fetch_supply_share(
                                    i, region, var, production_variables
                                )
                                supply_share *= ratio

                            except KeyError:
                                print(
                                    f"Could not find suppliers for {name}, {ref_prod}, from {region}"
                                )
                                continue

                            if supply_share > 0:
                                suppliers = get_shares_from_production_volume(
                                    potential_suppliers
                                )

                                new_excs.extend(
                                    self.write_suppliers_exchanges(
                                        suppliers, float(supply_share)
                                    )
                                )

                                if "is fuel" in market_vars:
                                    if region not in isfuel:
                                        isfuel[region] = {}
                                    isfuel[region].update(
                                        {
                                            pathway: {
                                                f: val * supply_share
                                                for f, val in market_vars["is fuel"][
                                                    pathway
                                                ].items()
                                            }
                                        }
                                    )

                        if len(new_excs) > 0:
                            total = 0

                            for exc in new_excs:
                                total += exc["amount"]
                            for exc in new_excs:
                                exc["amount"] /= total
                                if waste_market:
                                    # if this is a waste market, we need to
                                    # flip the sign of the amount
                                    exc["amount"] *= -1

                            new_market["exchanges"].extend(new_excs)

                            # check if we should add some additional exchanges
                            if "add" in market_vars:
                                for additional_exc in market_vars["add"]:
                                    add_excs = self.add_additional_exchanges(
                                        additional_exc,
                                        region,
                                        dp.descriptor["ecoinvent"]["version"],
                                    )
                                    new_market["exchanges"].extend(add_excs)

                            # check if there are variables that
                            # relate to inefficiencies or losses

                            if "efficiency" in market_vars:
                                efficiency_data = self.external_scenarios_data[i][
                                    "efficiency"
                                ]
                                new_market = self.adjust_efficiency_of_new_markets(
                                    new_market, market_vars, region, efficiency_data
                                )

                            self.database.append(new_market)
                            self.write_log(new_market)
                            self.add_to_index(new_market)
                            market_status[region] = True

                        else:
                            print(
                                f"No suppliers found for {new_market['name']} in {region}. "
                                "No market created. This may cause linking issue."
                            )
                            market_status[region] = False

                    # if there's more than one region,
                    # we create a World region
                    create_world_region = True
                    if (
                        "World" in regions
                        or "World" in market_vars.get("except regions", [])
                        or len(regions) == 1
                    ):
                        create_world_region = False

                    if create_world_region:
                        world_market = self.fill_in_world_market(
                            market=market_vars,
                            regions=regions,
                            i=i,
                            pathways=production_variables,
                            waste_market=waste_market,
                        )
                        self.database.append(world_market)
                        self.write_log(world_market)
                        self.add_to_index(world_market)

                        regions.append("World")
                        market_status["World"] = True

                    # if the new markets are meant to replace for other
                    # providers in the database

                    if "replaces" in market_vars:
                        self.relink_to_new_datasets(
                            replaces=market_vars["replaces"],
                            replaces_in=market_vars.get("replaces in", None),
                            new_name=market_vars["name"],
                            new_ref=market_vars["reference product"],
                            ratio=market_vars.get("replacement ratio", 1),
                            regions=regions,
                            waste_process=waste_market,
                            isfuel=isfuel,
                            market_status=market_status,
                        )

    def relink_to_new_datasets(
        self,
        replaces: list,
        replaces_in: list,
        new_name: str,
        new_ref: str,
        ratio,
        regions: list,
        waste_process: bool = False,
        isfuel: dict = None,
        market_status: dict = None,
    ) -> None:
        """
        Replaces exchanges that match `old_name` and `old_ref` with exchanges that
        have `new_name` and `new_ref`. The new exchange is from an IAM region, and so, if the
        region is not part of `regions`, we use `World` instead.

        :param new_name: `name`of the new provider
        :param new_ref: `product` of the new provider
        :param regions: list of IAM regions the new provider can originate from
        :param ratio: ratio of the new provider to the old provider
        :param replaces: list of dictionaries with `name`, `product`, `location`, `unit` of the old provider
        :param replaces_in: list of dictionaries with `name`, `product`, `location`, `unit` of the datasets to replace
        :param waste_process: True if the process is a waste process
        :param isfuel: True if the process is a fuel process
        :param market_status: dictionary with regions as keys and True/False as values

        """

        # filter out regions from `regions`for which
        # no market has been created
        if market_status:
            regions = [r for r in regions if market_status.get(r) is True]

        datasets = []
        exchanges_replaced = []
        fuel_amount = 0

        if replaces_in is not None:
            for k in replaces_in:
                list_fltr = []
                operator = k.get("operator", "equals")
                for field in ["name", "reference product", "location", "unit"]:
                    if field in k:
                        if field == "location":
                            list_fltr.append(ws.equals(field, k[field]))
                        else:
                            if operator == "equals":
                                list_fltr.append(ws.equals(field, k[field]))
                            else:
                                list_fltr.append(ws.contains(field, k[field]))
                            list_fltr.append(ws.contains(field, k[field]))
                datasets.extend(list(ws.get_many(self.database, *list_fltr)))
        else:
            datasets = self.database

        # also filter out datasets that
        # have the same name and ref product
        # as new_name and new_ref
        datasets = [
            d
            for d in datasets
            if not (d["name"] == new_name and d["reference product"] == new_ref)
        ]

        datasets = [
            d
            for d in datasets
            if (d["name"], d["reference product"])
            not in [(x["name"], x["product"]) for x in replaces]
        ]

        list_fltr = []
        for k in replaces:
            fltr = []
            operator = k.get("operator", "equals")
            for field in ["name", "product", "location", "unit"]:
                if field in k:
                    if field == "location":
                        fltr.append(ws.equals(field, k[field]))
                    else:
                        if operator == "equals":
                            fltr.append(ws.equals(field, k[field]))
                        else:
                            fltr.append(ws.contains(field, k[field]))
            list_fltr.append(fltr)

        for dataset in datasets:
            filtered_exchanges = []
            for fltr in list_fltr:
                filtered_exchanges.extend(list(ws.technosphere(dataset, *fltr)))

            # remove filtered exchanges from the dataset
            dataset["exchanges"] = [
                exc for exc in dataset["exchanges"] if exc not in filtered_exchanges
            ]

            new_exchanges = []

            for exc in filtered_exchanges:
                if (
                    exc["location"] in regions
                    and new_name == exc["name"]
                    and new_ref == exc["product"]
                ):
                    new_exchanges.append(exc)
                    continue

                new_loc = None
                exchanges_replaced.append(
                    (
                        exc["name"],
                        exc["product"],
                        exc["location"],
                    )
                )
                if len(regions) == 1:
                    new_loc = regions[0]

                elif dataset["location"] in regions:
                    new_loc = dataset["location"]

                elif self.geo.ecoinvent_to_iam_location(dataset["location"]) in regions:
                    new_loc = self.geo.ecoinvent_to_iam_location(dataset["location"])

                elif dataset["location"] in ["GLO", "RoW"]:
                    if "World" in regions:
                        new_loc = "World"

                    else:
                        new_loc = self.find_best_substitute_suppliers(
                            new_name, new_ref, regions
                        )

                if isinstance(new_loc, str):
                    new_loc = [(new_loc, 1.0)]

                if new_loc:
                    for loc, share in new_loc:
                        # add new exchange
                        new_exchanges.append(
                            {
                                "amount": exc["amount"] * ratio * share,
                                "type": "technosphere",
                                "unit": exc["unit"],
                                "location": loc,
                                "name": new_name,
                                "product": new_ref,
                            }
                        )

                        if isfuel:
                            fuel_amount += exc["amount"] * ratio * share
                else:
                    new_exchanges.append(exc)

            if len(filtered_exchanges) > 1:
                # sum up exchanges with the same name, product, and location
                new_exchanges = self.sum_exchanges(new_exchanges)

            # if it is a fuel process, we need to modify biogenic and fossil CO2
            # emissions of processes receiving that new fuel.
            if isfuel:
                if len(filtered_exchanges) > 0:
                    if dataset["location"] in isfuel:
                        bio_co2 = sum(
                            x["Carbon dioxide, non-fossil"] * fuel_amount
                            for x in isfuel[dataset["location"]].values()
                        )
                        if (
                            len(
                                list(
                                    ws.biosphere(
                                        dataset,
                                        ws.equals("name", "Carbon dioxide, non-fossil"),
                                    )
                                )
                            )
                            == 0
                        ):
                            dataset["exchanges"].append(
                                {
                                    "name": "Carbon dioxide, non-fossil",
                                    "amount": bio_co2,
                                    "unit": "kilogram",
                                    "type": "biosphere",
                                    "input": (
                                        "biosphere3",
                                        self.biosphere_flows[
                                            (
                                                "Carbon dioxide, non-fossil",
                                                "air",
                                                "unspecified",
                                                "kilogram",
                                            )
                                        ],
                                    ),
                                }
                            )
                        else:
                            for exc in ws.biosphere(
                                dataset, ws.equals("name", "Carbon dioxide, non-fossil")
                            ):
                                exc["amount"] += bio_co2

                        for exc in ws.biosphere(
                            dataset, ws.equals("name", "Carbon dioxide, fossil")
                        ):
                            exc["amount"] -= bio_co2

            dataset["exchanges"].extend(new_exchanges)
            fuel_amount = 0

        # if no "replaces in" is given, we consider that the dataset to
        # be replaced should be emptied and a link to the new dataset
        # should be added

        if not replaces_in and len(exchanges_replaced) > 0:
            unique_exchanges_replaced = list(set(exchanges_replaced))
            # keep tuples in the list
            # whose third items returns True
            # for market_status[item[2]]
            unique_exchanges_replaced = [
                x for x in unique_exchanges_replaced if market_status.get(x[2]) is True
            ]

            if len(unique_exchanges_replaced) > 0:
                name = unique_exchanges_replaced[0][0]
                ref = unique_exchanges_replaced[0][1]
                locs = [x[2] for x in unique_exchanges_replaced]

                for ds in ws.get_many(
                    self.database,
                    ws.equals("name", name),
                    ws.equals("reference product", ref),
                    ws.either(*[ws.equals("location", l) for l in locs]),
                ):
                    # remove all exchanges except production exchanges
                    ds["exchanges"] = [
                        exc for exc in ds["exchanges"] if exc["type"] == "production"
                    ]
                    # add an exchange from a new supplier
                    if ds["location"] in ["GLO", "RoW"] and "World" in regions:
                        new_loc = "World"
                    elif ds["location"] in regions:
                        new_loc = ds["location"]
                    elif self.geo.ecoinvent_to_iam_location(ds["location"]) in regions:
                        new_loc = self.geo.ecoinvent_to_iam_location(ds["location"])
                    else:
                        new_loc = self.find_best_substitute_suppliers(
                            new_name, new_ref, regions
                        )

                    if isinstance(new_loc, str):
                        new_loc = [(new_loc, 1.0)]

                    for loc, share in new_loc:
                        ds["exchanges"].append(
                            {
                                "amount": 1.0
                                * ratio
                                * share
                                * (-1.0 if waste_process else 1.0),
                                "type": "technosphere",
                                "unit": ds["unit"],
                                "location": loc,
                                "name": new_name,
                                "product": new_ref,
                            }
                        )

    def sum_exchanges(self, dataset_exchanges):
        # sum up exchanges with the same name, product, and location
        new_exc = defaultdict(float)
        for exc in dataset_exchanges:
            key = (
                exc["name"],
                exc.get("product"),
                exc.get("categories"),
                exc.get("location"),
                exc.get("unit"),
                exc.get("input"),
                exc.get("type"),
            )
            new_exc[key] += exc["amount"]

        return [
            {
                "name": name,
                "product": product,
                "categories": categories,
                "location": location,
                "unit": unit,
                "input": input,
                "type": exc_type,
                "amount": amount,
            }
            for (
                name,
                product,
                categories,
                location,
                unit,
                input,
                exc_type,
            ), amount in new_exc.items()
        ]

    def find_best_substitute_suppliers(self, new_name, new_ref, regions):
        # find the best suppliers for new dataset
        # if there are several suppliers with the same name and product, the one
        # with the highest production volume is chosen
        suppliers = get_shares_from_production_volume(
            list(
                ws.get_many(
                    self.database,
                    ws.equals("name", new_name),
                    ws.equals("reference product", new_ref),
                    ws.either(*[ws.equals("location", r) for r in regions]),
                )
            )
        )
        return [(x[1], y) for x, y in suppliers.items()]

    def write_log(self, dataset, status="created"):
        """
        Write log file.
        """

        logger.info(
            f"{status}|{self.model}|{self.scenario}|{self.year}|"
            f"{dataset['name']}|{dataset['location']}|"
            f"{dataset.get('log parameters', {}).get('technosphere scaling factor')}|"
            f"{dataset.get('log parameters', {}).get('biosphere scaling factor')}"
        )
