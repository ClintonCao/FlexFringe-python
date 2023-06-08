import os
import subprocess
import shutil
import time
from pathlib import Path
from PIL import Image
import graphviz as graphviz
import io
import pandas as pd
from multipledispatch import dispatch
from pandas import DataFrame
from tempfile import NamedTemporaryFile
import warnings
from loguru import logger

logger.disable("flexfringe")


class FlexFringe:
    # namespace for multipledispatch
    namespace = dict()

    def __init__(self, flexfringe_path=None, **kwargs):
        """
        Initialize the flexfringe wrapper
        :param flexfringe_path: Path to flexfringe, or None to autodetect (flexfringe must be in PATH)
        :param kwargs: Any keyword arguments will be passed to flexfringe in the form of --key=value
        """

        if flexfringe_path is None:
            self.path = shutil.which("flexfringe")
        else:
            self.path = flexfringe_path

        if self.path is None:
            raise RuntimeError(
                "Could not find flexfringe executable. Please put it in your PATH or provide flexfringe_path in the constructor")

        self.tracefile = None
        self.resultfile = None

        self.kwargs = kwargs

    @property
    def dot_out(self) -> Path:
        return self._get_out_file(".ff.final.dot")

    @property
    def json_out(self) -> Path:
        return self._get_out_file(".ff.final.json")

    @property
    def result_out(self) -> Path:
        return self._get_out_file(".ff.final.json.result.csv")

    def _get_out_file(self, extension: str) -> Path:
        if self.tracefile is None:
            raise RuntimeError("No tracefile specified. Please first run \"fit\"")

        tmp = Path(f"{self.tracefile}{extension}")

        if not tmp.exists() or not tmp.is_file():
            raise RuntimeError(f"Could not find valid flexfringe output file at: {str(tmp)}")

        return tmp

    @dispatch(DataFrame, namespace=namespace)
    def fit(self, df: DataFrame, **kwargs):
        """
        Convenience method of fit which takes a pandas dataframe.
        First it writes the dataframe to a temporary csv file, and then it calls flexfringe like normal.
        Finally, it cleans up the temporary file

        :param df: Pandas dataframe containing the data to learn a state machine from
        :param kwargs: other parameters to be passed to flexfringe
        """
        with NamedTemporaryFile("w", suffix=".csv", delete=False) as file:
            df.to_csv(file)
            file.close()
            self.fit(file.name, **kwargs)
            os.remove(file.name)

    @dispatch(object, namespace=namespace)
    def fit(self, tracefile, **kwargs):
        """
        Calls flexfringe on the file path specified by tracefile
        kwargs are passed through to flexfringe in the form of --key=value

        :param tracefile: Path to the trace file to load. Can be either in abbadingo or csv format
        :param kwargs: other parameters to be passed to flexfringe
        """
        # Use the kwargs passed to this function as overrides for the ones specified in the constructor
        all_kwargs = dict(self.kwargs)
        for k, v in kwargs.items():
            all_kwargs[k] = v
        flags = self._format_kwargs(**all_kwargs)

        command = [tracefile] + flags

        self._run(command)

        self.tracefile = tracefile

        try:
            with self.dot_out.open('r') as fh:
                dot_content = fh.read()
            with self.json_out.open('r') as fh:
                json_content = fh.read()
        except FileNotFoundError as e:
            raise RuntimeError(f"Error running FlexFringe: no output file found: {e.filename}")
        
    @dispatch(object, namespace=namespace)
    def fit(self, tracefile, output_file=None, output_format=None, **kwargs):
        """
        Hacky function for learning a model from the tracefile and saving the model
        with a different name.

        kwargs are passed through to flexfringe in the form of --key=value

        :param tracefile: Path to the trace file to load. Can be either in abbadingo or csv format
        :param output_file: Path to where the model is saved
        :param kwargs: other parameters to be passed to flexfringe
        """
        # Use the kwargs passed to this function as overrides for the ones specified in the constructor
        all_kwargs = dict(self.kwargs)
        for k, v in kwargs.items():
            all_kwargs[k] = v
        flags = self._format_kwargs(**all_kwargs)

        command = [tracefile] + flags

        self._run(command)

        self.tracefile = tracefile

        model = output_file + '.final.' + output_format
        
        try:
            with open(model, 'r') as fh:
                _ = fh.read()
        except FileNotFoundError as e:
            raise RuntimeError(f"Error running FlexFringe: no output file found: {e.filename}")


    @dispatch(DataFrame)
    def predict(self, df: DataFrame, **kwargs):
        """
        Convenience method for predict which takes a pandas dataframe

        :param df: The pandas dataframe to write to csv and pass to flexfringe
        :param kwargs: other parameters to be passed to flexfringe
        :return: A dataframe with the output from flexfringe
        """
        with NamedTemporaryFile("w", suffix=".csv", delete=False) as file:
            df.to_csv(file)
            file.close()
            df_out = self.predict(file.name, **kwargs)
            os.remove(file.name)
            return df_out
    
    @dispatch(object)
    def predict(self, tracefile, **kwargs):
        """
        Runs flexfringe in predict mode, using the aptafile generated by a previous call to fit

        :param tracefile: the tracefile to run predictions on
        :param kwargs: other parameters to be passed to flexfringe
        :return: A dataframe with the output from flexfringe
        """
        # Use the kwargs passed to this function as overrides for the ones specified in the constructor
        all_kwargs = dict(self.kwargs)
        for k, v in kwargs.items():
            all_kwargs[k] = v
        flags = self._format_kwargs(**all_kwargs)

        command = [tracefile, "--mode=predict", f"--aptafile={self.json_out}"] + flags

        self._run(command)

        return self._parse_flexfringe_result()

    @dispatch(str, str)
    def predict(self, tracefile:str, apta_file:str, **kwargs):
        """
        Runs flexfringe in predict mode, using the provided apta file

        :param tracefile: the tracefile to run predictions on
        :param apta_file: the apta file to use for predictions
        :param kwargs: other parameters to be passed to flexfringe
        :return: A dataframe with the output from flexfringe
        """
        # Use the kwargs passed to this function as overrides for the ones specified in the constructor
        all_kwargs = dict(self.kwargs)
        for k, v in kwargs.items():
            all_kwargs[k] = v
        flags = self._format_kwargs(**all_kwargs)
        
        command = [tracefile, "--mode=predict", f"--aptafile={apta_file}"] + flags
        self.tracefile = apta_file.split('.ff')[0] # training file is used for writing prediction results
        self._run(command)

        return self._parse_flexfringe_result()

    def _parse_flexfringe_result(self):
        df = pd.read_csv(self.result_out, delimiter=";")
        df.columns = [column.strip() for column in df.columns]

        # Parse abbadingo traces
        abd_traces = df['abbadingo trace']
        abd_traces = abd_traces.apply(lambda x: x.strip().strip("\""))

        abd_type = []
        abd_len = []
        abd_trc = []

        for abd_trace in abd_traces:
            parts = abd_trace.split(" ")
            abd_type.append(parts[0])
            abd_len.append(parts[1])
            abd_trc.append(parts[2:])

        df = df.drop(columns=["abbadingo trace"])
        df.insert(1, "abbadingo type", abd_type)
        df.insert(2, "abbadingo length", abd_len)
        df.insert(3, "abbadingo trace", abd_trc)

        # Parse state sequences
        df['state sequence'] = df['state sequence'] \
            .apply(lambda x: x.strip().strip("[").strip("]").split(","))

        # Parse score sequence
        df['score sequence'] = df['score sequence'] \
            .apply(lambda x: [float(val) for val in x.strip().strip("[").strip("]").split(",")])

        # And the rest of the score columns
        df['sum scores'] = df['sum scores'].astype(float)
        df['mean scores'] = df['mean scores'].astype(float)
        df['min score'] = df['min score'].astype(float)

        return df

    def _run(self, command=None):
        """
        Wrapper to call the flexfringe binary
        """

        if command is None:
            command = ["--help"]

        full_cmd = ["flexfringe"] + command
        logger.debug(f"Running: {' '.join(full_cmd)}")
        result = subprocess.run([self.path] + command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True)
        logger.debug(f"Flexfringe exit code: {result.returncode}")
        logger.info(f"Flexfringe stdout:\n{result.stdout}")
        logger.info(f"Flexfringe stderr:\n{result.stderr}")

    def show(self, format="png"):
        """
        Renders the final state machine generated by flexfringe using graphviz
        and displays it using pillow.

        :param format: a file format supported by both graphviz and pillow.
        """
        if shutil.which("dot") is None:
            raise RuntimeError("pfind dot executable in path. Displaying graphs will not work. "
                               "Please install graphviz: https://graphviz.org/download/")

        if self.dot_out is None:
            raise RuntimeError("No output available, run \"fit\" first")
        else:
            with Path(self.dot_out).open('r') as in_file:
                g = graphviz.Source(
                    in_file.read()
                )

            data = io.BytesIO()
            data.write(g.pipe(format=format))
            data.seek(0)

            img = Image.open(data)
            img.show()

            # Need to give it time to open image viewer :/
            time.sleep(1)

    def _format_kwargs(self, **kwargs):
        """
        Turns kwargs into a list of command line flags that flexfringe understands
        :param kwargs: the kwargs to translate
        :return: a list of command line args for flexfringe
        """
        flags = []
        for key in kwargs:
            flags += [f"--{key}={kwargs[key]}"]
        return flags