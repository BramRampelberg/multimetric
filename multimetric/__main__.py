# SPDX-FileCopyrightText: 2023 Konrad Weihmann
# SPDX-License-Identifier: Zlib

import argparse
import json
import logging
import multiprocessing as mp
import os
import sys
import textwrap

import chardet
from pygments import lexers

from multimetric.cls.importer.filtered import FilteredImporter
from multimetric.cls.importer.pick import importer_pick
from multimetric.cls.modules import get_additional_parser_args
from multimetric.cls.modules import get_modules_calculated
from multimetric.cls.modules import get_modules_metrics
from multimetric.cls.modules import get_modules_stats


def ArgParser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        prog="multimetric", description='Calculate code metrics in various languages',
        epilog=textwrap.dedent("""
        Currently you could import files of the following types for --warn_* or --coverage

        Following information can be read

            <file> = full path to file
            <severity> = severity [error, warning, info]
            <content> = optional string

            Note: you could also add a single line, then <content>
                has to be a number reflecting to total number of findings

        File formats

        csv: CSV file of following line format
             <file>,<severity>,[<content>]

        json: JSON file
             <file>: {
                 ["content": <content>,]
                 "severity": <severity>
             }
        """))
    parser.add_argument(
        "--warn_compiler",
        default=None,
        help="File(s) holding information about compiler warnings")
    parser.add_argument(
        "--warn_duplication",
        default=None,
        help="File(s) holding information about code duplications")
    parser.add_argument(
        "--warn_functional",
        default=None,
        help="File(s) holding information about static code analysis findings")
    parser.add_argument(
        "--warn_standard",
        default=None,
        help="File(s) holding information about language standard violations")
    parser.add_argument(
        "--warn_security",
        default=None,
        help="File(s) File(s) holding information about found security issue")
    parser.add_argument(
        "--coverage",
        default=None,
        help="File(s) with compiler warningsFile(s) holding information about testing coverage")
    parser.add_argument(
        "--dump",
        default=False,
        action="store_true",
        help="Just dump the token tree")
    parser.add_argument(
        "--verbose",
        default=False,
        action="store_true",
        help="Verbose logging output")
    parser.add_argument(
        "--jobs",
        type=int,
        default=mp.cpu_count(),
        help="Run x jobs in parallel")
    get_additional_parser_args(parser)
    parser.add_argument("paths", nargs='+', help="File(s) or folder(s) to parse")
    parser.add_argument(
        "--ext",
        type=str,
        default=None,
        help="Required for folders. The file extension to process (e.g., .py, .swift)")
    return parser


def parse_args(*args):
    RUNARGS = ArgParser().parse_args(*args)
    RUNARGS.paths = [os.path.abspath(x) for x in RUNARGS.paths]

    is_folder_present = any(os.path.isdir(p) for p in RUNARGS.paths)
    if is_folder_present and not RUNARGS.ext:
        sys.exit("Error: --ext is required when providing a folder path.")

    # Setup logging
    stdout_log = logging.getLogger('stdout')
    stdout_log.setLevel(logging.DEBUG if RUNARGS.verbose else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG if RUNARGS.verbose else logging.INFO)
    formatter = logging.Formatter('%(message)s')
    handler.setFormatter(formatter)
    stdout_log.addHandler(handler)

    stderr_log = logging.getLogger('stderr')
    stderr_log.setLevel(logging.DEBUG if RUNARGS.verbose else logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG if RUNARGS.verbose else logging.INFO)
    formatter = logging.Formatter('%(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    stderr_log.addHandler(handler)

    return RUNARGS


def discover_files(paths, extension):
    """
    Scans a list of paths, finds files matching the extension in folders,
    and returns a flat list of unique file paths.
    """
    files_to_process = []
    
    # If no extension is specified, only process files provided directly.
    if not extension:
        for path in paths:
            if os.path.isfile(path):
                files_to_process.append(path)
        return files_to_process

    # Ensure the extension starts with a dot for consistent matching.
    if not extension.startswith('.'):
        extension = '.' + extension

    for path in paths:
        if os.path.isfile(path):
            # If a file is given directly, check if it matches the extension.
            if path.endswith(extension):
                files_to_process.append(path)
        elif os.path.isdir(path):
            # If a directory is given, walk through it recursively.
            for root, _, filenames in os.walk(path):
                for filename in filenames:
                    if filename.endswith(extension):
                        files_to_process.append(os.path.join(root, filename))
    
    # Return a sorted list of unique file paths.
    return list(sorted(set(files_to_process)))


def file_process(_file, _args, _importer):
    res = {}
    store = {}
    try:
        _lexer = lexers.get_lexer_for_filename(_file)
    except ValueError:
        logging.getLogger('stderr').error(
            f'The file {_file} could not be identified automatically. Skipping this file.')
        return ({}, _file, 'lexer.error', [], {})
    try:
        with open(_file, "rb") as i:
            _raw_cnt = i.read()
            _enc = chardet.detect(_raw_cnt)
            # This is the full source code text as a UTF-8 encoded byte string.
            _cnt = _raw_cnt.decode(_enc["encoding"]).encode("utf-8")
        _localImporter = {k: FilteredImporter(
            v, _file) for k, v in _importer.items()}
        
        # NOTE: The full source code is in `_cnt`. If you have a custom LOC metric,
        # you can pass the decoded string to it like this:
        # source_text = _cnt.decode('utf-8')
        # x.parse_tokens(_lexer.name, tokens, source_text=source_text)
        
        tokens = list(_lexer.get_tokens(_cnt))
        if _args.dump:
            for x in tokens:
                print(f"{_file}: {x[0]} -> {repr(x[1])}")
                logging.getLogger('stdout').info(f"{_file}: {x[0]} -> {repr(x[1])}")
        else:
            _localMetrics = get_modules_metrics(_args, **_localImporter)
            _localCalc = get_modules_calculated(_args, **_localImporter)
            for x in _localMetrics:
                x.parse_tokens(_lexer.name, tokens)
                res.update(x.get_results())
                store.update(x.get_internal_store())
            for x in _localCalc:
                res.update(x.get_results(res))
                store.update(x.get_internal_store())
    except Exception as e:
        logging.getLogger('stderr').exception(e)
        tokens = []
    return (res, _file, _lexer.name, tokens, store)


def run(_args):
    _result = {"files": {}, "overall": {}}
    
    files_to_process = discover_files(_args.paths, _args.ext)
    if not files_to_process:
        logging.getLogger('stderr').error("No files found to process with the given criteria.")
        return _result

    logging.getLogger('stdout').info(f"Found {len(files_to_process)} file(s) to analyze.")

    # Get importer
    _importer = {}
    _importer["import_compiler"] = importer_pick(_args, _args.warn_compiler)
    _importer["import_coverage"] = importer_pick(_args, _args.coverage)
    _importer["import_duplication"] = importer_pick(
        _args, _args.warn_duplication)
    _importer["import_functional"] = importer_pick(
        _args, _args.warn_functional)
    _importer["import_security"] = importer_pick(_args, _args.warn_standard)
    _importer["import_standard"] = importer_pick(_args, _args.warn_security)
    # sanity check
    _importer = {k: v for k, v in _importer.items() if v}

    # instance metric modules
    _overallMetrics = get_modules_metrics(_args, **_importer)
    _overallCalc = get_modules_calculated(_args, **_importer)

    with mp.Pool(processes=_args.jobs) as pool:
        results = [pool.apply(file_process, args=(
            f, _args, _importer)) for f in files_to_process]

    for x in results:
        _result["files"][x[1]] = x[0]

    for y in _overallMetrics:
        _result["overall"].update(
            y.get_results_global([x[4] for x in results]))
    for y in _overallCalc:
        _result["overall"].update(y.get_results(_result["overall"]))
    for m in get_modules_stats(_args, **_importer):
        _result = m.get_results(_result, "files", "overall")

    def round_float(item):
        if isinstance(item, dict):
            for k, v in item.items():
                item[k] = round_float(v)
        elif isinstance(item, list):
            for index, value in enumerate(item):
                item[index] = round_float(value)
        elif isinstance(item, float):
            item = round(item, 3)
        return item

    return round_float(_result)


def main():  # pragma: no cover
    _args = parse_args()
    _result = run(_args)
    if not _args.dump and _result and _result.get("files"):
        # Output
        logging.getLogger('stdout').info(json.dumps(_result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()