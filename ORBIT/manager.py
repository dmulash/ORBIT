__author__ = ["Jake Nunemaker"]
__copyright__ = "Copyright 2019, National Renewable Energy Laboratory"
__maintainer__ = "Jake Nunemaker"
__email__ = ["jake.nunemaker@nrel.gov"]


import re
import datetime as dt
import collections.abc as collections
from math import ceil

import pandas as pd

from ORBIT import library
from ORBIT.library import initialize_library, extract_library_data
from ORBIT.phases.design import (
    MonopileDesign,
    ArraySystemDesign,
    ExportSystemDesign,
    ProjectDevelopment,
    ScourProtectionDesign,
    OffshoreSubstationDesign,
)
from ORBIT.phases.install import (
    TurbineInstallation,
    MonopileInstallation,
    ArrayCableInstallation,
    ExportCableInstallation,
    ScourProtectionInstallation,
    OffshoreSubstationInstallation,
)
from ORBIT.simulation.exceptions import PhaseNotFound, WeatherProfileError


class ProjectManager:
    """Base Project Manager Class."""

    date_format_short = "%m/%d/%Y"
    date_format_long = "%m/%d/%Y %H:%M"

    _design_phases = [
        ProjectDevelopment,
        MonopileDesign,
        ArraySystemDesign,
        ExportSystemDesign,
        ScourProtectionDesign,
        OffshoreSubstationDesign,
    ]

    _install_phases = [
        MonopileInstallation,
        TurbineInstallation,
        OffshoreSubstationInstallation,
        ArrayCableInstallation,
        ExportCableInstallation,
        ScourProtectionInstallation,
    ]

    def __init__(self, config, library_path=None, weather=None):
        """
        Creates and instance of ProjectManager.

        Parameters
        ----------
        config : dict
            Project configuration.
        library_path: str, default: None
            The absolute path to the project library.
        weather : pd.DataFrame
            Site weather.
        """

        initialize_library(library_path)
        self.config = extract_library_data(
            config,
            additional_keys=[
                *config.get("design_phases", []),
                *config.get("install_phases", []),
            ],
        )

        self.weather = weather
        self.phase_times = {}
        self.phase_costs = {}
        self._output_dfs = {}

        self.design_results = {}
        self.detailed_outputs = {}

    def run_project(self):
        """
        Main project run method.

        Parameters
        ----------
        self.config['design_phases'] : list
            Defines which design phases are ran. These phases are ran before
            install phases and merge the result of the design into self.config.
        self.config['install_phases'] : list | dict
            Defines which installation phases are ran.

            - If ``self.config['install_phases']`` is a list, phases are ran
              sequentially using ``self.run_multiple_phases_in_serial()``.
            - If ``self.config['install_phases']`` is a dict, phases are ran
              using ``self.run_multiple_phases_overlapping()``. The expected
              format for the dictionary is ``{'phase_name': '%m/%d/%Y'}``.
        """

        design_phases = self.config.get("design_phases", [])
        install_phases = self.config.get("install_phases", [])

        if isinstance(design_phases, str):
            design_phases = [design_phases]
        if isinstance(install_phases, str):
            install_phases = [install_phases]

        self.run_all_design_phases(design_phases)

        if isinstance(install_phases, list):
            self.run_multiple_phases_in_serial(install_phases)

        elif isinstance(install_phases, dict):
            self.run_multiple_phases_overlapping(install_phases)

    @classmethod
    def compile_input_dict(cls, phases):
        """
        Returns a compiled input dictionary given a list of phases to run.

        Parameters
        ----------
        phases : list
            A collection of offshore design or installation phases.
        """

        _phases = {p: cls.find_key_match(p) for p in phases}
        _error = [n for n, c in _phases.items() if not bool(c)]
        if _error:
            raise PhaseNotFound(_error)

        design_phases = {
            n: c for n, c in _phases.items() if hasattr(c, "_design_phase")
        }
        install_phases = {
            n: c for n, c in _phases.items() if hasattr(c, "_install_phase")
        }

        config = {}
        for i in install_phases.values():
            config = cls.merge_dicts(config, i.expected_config)

        for d in design_phases.values():
            config = cls.merge_dicts(config, d.expected_config)
            config = cls.remove_keys(config, d.output_config)

        config["design_phases"] = [*design_phases.keys()]
        config["install_phases"] = [*install_phases.keys()]

        return config

    @classmethod
    def find_key_match(cls, target):
        """
        Searches cls.phase_dict() for a key that matches text in 'target'.

        Parameters
        ----------
        target : str
            Phase name to search for a match with.

        Returns
        -------
        phase_class : BasePhase | None
            Matched module class or None if no match is found.
        """

        phase = re.split("[_ ]", target)[0]
        phase_dict = cls.phase_dict()
        phase_class = phase_dict.get(phase, None)

        return phase_class

    @classmethod
    def phase_dict(cls):
        """
        Returns dictionary of all possible phases with format 'name': 'class'.
        """

        install = {p.__name__: p for p in cls._install_phases}
        design = {p.__name__: p for p in cls._design_phases}

        return {**install, **design}

    @classmethod
    def merge_dicts(cls, left, right, overwrite=True, add_keys=True):
        """
        Merges two dicts (right into left) with an option to add keys of right.

        Parameters
        ----------
        left : dict
        right : dict
        add_keys : bool

        Returns
        -------
        new : dict
            Merged dictionary.
        """
        new = left.copy()
        if not add_keys:
            right = {k: right[k] for k in set(new).intersection(set(right))}

        for k, _ in right.items():
            if (
                k in new
                and isinstance(new[k], dict)
                and isinstance(right[k], collections.Mapping)
            ):
                new[k] = cls.merge_dicts(
                    new[k], right[k], overwrite=overwrite, add_keys=add_keys
                )
            else:
                if overwrite or k not in new:
                    new[k] = right[k]

                else:
                    continue

        return new

    @classmethod
    def remove_keys(cls, left, right):
        """
        Recursively removes keys from left that are found in right.

        Parameters
        ----------
        left : dict
        right : dict

        Returns
        -------
        new : dict
            Left dictionary with keys of right removed.
        """

        new = left.copy()
        right = {k: right[k] for k in set(new).intersection(set(right))}

        for k, val in right.items():

            if isinstance(new.get(k, None), dict) and isinstance(val, dict):
                new[k] = cls.remove_keys(new[k], val)

                if not new[k]:
                    _ = new.pop(k)

            else:
                _ = new.pop(k)

        return new

    def create_config_for_phase(self, phase):
        """
        Produces a configuration input dictionary for 'phase'.

        This method will pick the most specific definition of each parameter.
        For example, if self.master_config['site']['distance'] and
        self.config['PhaseName']['site']['distance'] are both defined,
        the latter will be chosen as it is more specific. This allows for phase
        specific definitions, eg. distance to port dependent on phase.

        Parameters
        ----------
        phase : str
            Name of phase. Phase specific information will be pulled from
            self.config['PhaseName'] if this key exists.

        Returns
        -------
        phase_config : dict
            Configuration dictionary with phase specific information merged in.
        """

        _specific = self.config.get(phase, {}).copy()
        _general = {
            k: v
            for k, v in self.config.items()
            if k not in set(self.phase_dict())
        }

        phase_config = self.merge_dicts(_general, _specific)

        return phase_config

    def run_install_phase(self, name, weather):
        """
        Compiles the phase specific configuration input dictionary for input
        'name', checks the input against _class.expected_config and runs the
        phase calculations with 'phase.run()'.

        Parameters
        ----------
        name : str
            Phase to run.
        weather : bool | DataFrame

        Returns
        -------
        time : int | float
            Total phase time.
        cost : int | float
            Total phase cost.
        df : pd.DataFrame
            Total phase dataframe.
        """

        _class = self.get_phase_class(name)
        _config = self.create_config_for_phase(name)

        kwargs = _config.pop("kwargs", {})
        phase = _class(_config, weather=weather, phase_name=name, **kwargs)
        phase.run()

        time = phase.total_phase_time
        cost = phase.total_phase_cost
        df = phase.phase_dataframe

        self.detailed_outputs[name] = phase.detailed_output

        return time, cost, df

    def get_phase_class(self, phase):
        """
        Returns the class object for input 'phase'.

        Parameters
        ----------
        phase : str
            Name of phase. Must match a class name in either
            'self._install_phases' or 'self._design_phases'.

        Returns
        -------
        phase_class : Phase
            Class of base type Phase that represents input 'phase'.
        """

        _dict = self.phase_dict()
        phase_class = self.find_key_match(phase)
        if phase_class is None:
            raise PhaseNotFound(phase)

        return phase_class

    def run_all_design_phases(self, phase_list):
        """
        Runs multiple design phases and adds '.design_result' to self.config.
        """

        for name in phase_list:
            self.run_design_phase(name)

    def run_design_phase(self, name):
        """
        Runs a design phase defined by 'name' and merges the '.design_result'
        into self.config.

        Parameters
        ----------
        name : str
            Name of design phase that partially matches a key in `phase_dict`.
        """

        _class = self.get_phase_class(name)
        _config = self.create_config_for_phase(name)

        phase = _class(_config)
        phase.run()

        self.phase_costs[name] = phase.total_phase_cost
        self.phase_times[name] = phase.total_phase_time
        self.design_results = self.merge_dicts(
            self.design_results, phase.design_result, overwrite=False
        )

        self.config = self.merge_dicts(
            self.config, phase.design_result, overwrite=False
        )

    def run_multiple_phases_in_serial(self, phase_list):
        """
        Runs multiple phases listed in self.config['install_phases'] in serial.

        Parameters
        ----------
        phase_list : list
            List of installation phases to run.
        """

        _start = 0

        for name in phase_list:
            if self.weather is not None:
                weather = self.weather.iloc[ceil(_start) :].copy()

            else:
                weather = None

            time, cost, df = self.run_install_phase(name, weather)

            self.phase_times[name] = time
            self.phase_costs[name] = cost

            df["time"] = df["time"] + _start
            self._output_dfs[name] = df

            _start = ceil(_start + time)

    def run_multiple_phases_overlapping(self, phase_dict):
        """
        Runs multiple phases with defined start days in
        self.config['install_phases'].

        Parameters
        ----------
        phase_dict : dict
            Dictionary of phases to run with keys that indicate start date.
        """

        start_dates = {
            k: dt.datetime.strptime(v, self.date_format_short)
            for k, v in phase_dict.items()
        }

        _zero = min(start_dates.values())

        for name, start in start_dates.items():
            if self.weather is not None:
                weather = self.get_weather_profile(start)

            else:
                weather = None

            time, cost, df = self.run_install_phase(name, weather)

            self.phase_times[name] = time
            self.phase_costs[name] = cost

            df["time"] = df["time"] + (start - _zero).days * 24
            self._output_dfs[name] = df

    def get_weather_profile(self, start):
        """
        Pulls weather profile from 'self.weather' starting at 'start', raising
        any errors if needed.

        Parameters
        ----------
        start : datetime
            Starting index for output weather profile.

        Returns
        -------
        profile : DataFrame
            Weather profile with first index at 'start'.
        """

        if not isinstance(self.weather.index, pd.DatetimeIndex):
            self.weather.index = pd.to_datetime(self.weather.index)

        profile = self.weather.loc[start:].copy()

        if profile.empty:
            raise WeatherProfileError(start, self.weather)

        return profile

    @property
    def project_dataframe(self):
        """Returns total project schedule in DataFrame format."""

        if not self._output_dfs:
            raise Exception("Project has not been ran yet.")

        df = (
            pd.concat([df for _, df in self._output_dfs.items()])
            .sort_values("time")
            .reset_index(drop=True)
        )

        return df

    @staticmethod
    def create_input_xlsx():
        """
        A wrapper around self.compile_input_dict that produces an excel input
        file instead of a .json file.
        """
        pass

    @property
    def phase_dates(self):
        """
        Returns a combination of input start dates and `self.phase_times`.
        """

        if not isinstance(self.config["install_phases"], dict):
            print("Project was not configured with start dates.")
            return None

        dates = {}

        for phase, _start in self.config["install_phases"].items():

            start = dt.datetime.strptime(_start, self.date_format_short)
            end = start + dt.timedelta(hours=ceil(self.phase_times[phase]))

            dates[phase] = {
                "start": start.strftime(self.date_format_long),
                "end": end.strftime(self.date_format_long),
            }

        return dates

    def export_configuration(self, file_name):
        """
        Exports the configuration settings for the project to
        `library_path/project/config/file_name.yaml`.

        Parameters
        ----------
        file_name : str
            Name to use for the file.
        """

        library.export_library_specs("config", file_name, self.config)
