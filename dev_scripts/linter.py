#!/usr/bin/env python
"""
Reformat and lint python and ipynb files.

This script uses the version of the files present on the disk and not what is
staged for commit by git, thus you need to stage again after running it.

E.g.,
# Lint all modified files in git client.
> linter.py

# Lint current files.
> linter.py --current_git_files --collect_only
> linter.py --current_git_files --all

# Lint previous commit files.
> linter.py --previous_git_commit_files --collect_only

# Lint a certain number of previous commits
> linter.py --previous_git_commit_files n --collect_only
> linter.py --files event_study/*.py linter_v2.py --yapf --isort -v DEBUG

- To jump to all the warnings to fix:
> vim -c "cfile linter.log"

- Check all jupytext files.
> linter.py -d . --action sync_jupytext

# Run some test with:
> test_linter.sh
"""

# TODO(gp): Add mccabe score.
# TODO(gp): Do not overwrite file when there is no change.
# TODO(gp): Add autopep8 if useful?
# TODO(gp): Add vulture, snake_food
# TODO(gp): Save tarball, dir or patch of changes
# TODO(gp): Ensure all file names are correct (e.g., no space, nice TaskXYZ, no
# `-` but `_`...)
# TODO(gp): Make sure that there are no conflict markers.
# TODO(gp): Report number of errors vs warnings.
# TODO(gp): Test directory should be called "test" and not "tests"
# TODO(gp): Discourage checking in master
# TODO(gp): Python files should end with py
# TODO(gp): All and only executable python files (i.e., with main) should have
# #!/usr/bin/env python
# TODO(gp): Add https://github.com/PyCQA/flake8-bugbear

import argparse
import datetime
import itertools
import logging
import os
import py_compile
import re
import sys

import helpers.dbg as dbg
import helpers.git as git
import helpers.io_ as io_
import helpers.printing as printing
import helpers.system_interaction as si

_LOG = logging.getLogger(__name__)

# Use the current dir and not the dir of the executable.
_TMP_DIR = os.path.abspath(os.getcwd() + "/tmp.linter")

# #############################################################################
# Utils.
# #############################################################################


# TODO(gp): This could become the default behavior of system().
def _system(cmd, abort_on_error=True):
    suppress_output = _LOG.getEffectiveLevel() > logging.DEBUG
    rc = si.system(
        cmd,
        abort_on_error=abort_on_error,
        suppress_output=suppress_output,
        log_level=logging.DEBUG,
    )
    return rc


def _remove_empty_lines(output):
    output = [l for l in output if l.strip("\n") != ""]
    return output


def _filter_target_files(file_names):
    """
    Keep only the files that
    - have extension .py or .ipynb
    - are not Jupyter checkpoints
    - are not in tmp dirs
    """
    file_names_out = []
    for file_name in file_names:
        _, file_ext = os.path.splitext(file_name)
        is_valid = file_ext in (".py", ".ipynb")
        is_valid &= ".ipynb_checkpoints/" not in file_name
        # Skip files in directory starting with "tmp.".
        is_valid &= "/tmp." not in file_name
        if is_valid:
            file_names_out.append(file_name)
    return file_names_out


# TODO(gp): Horrible: to remove / rewrite.
def _clean_file(file_name, write_back):
    """
    Remove empty spaces, tabs, windows end-of-lines.
    :param write_back: if True the file is overwritten in place.
    """
    # Read file.
    file_in = []
    with open(file_name, "r") as f:
        for line in f:
            file_in.append(line)
    #
    file_out = []
    for line in file_in:
        # A line can be deleted if it has only spaces and \n.
        if not any(char not in (" ", "\n") for char in line):
            line = "\n"
        # dos2unix.
        line = line.replace("\r\n", "\n")
        file_out.append(line)
    # Remove whitespaces at the end of file.
    while file_out and (file_out[-1] == "\n"):
        # While the last item in the list is blank, removes last element.
        file_out.pop(-1)
    # Write the new the output to file.
    if write_back:
        file_in = "".join(file_in)
        file_out = "".join(file_out)
        if file_in != file_out:
            _LOG.debug("Writing back file '%s'", file_name)
            with open(file_name, "w") as f:
                f.write(file_out)
        else:
            _LOG.debug("No change in file, so no saving")
    return file_in, file_out


def _annotate_output(output, executable):
    """
    Annotate a list containing the output of a cmd line with the name of the
    executable used.
    :return: list of strings
    """
    dbg.dassert_isinstance(output, list)
    output = [t + " [%s]" % executable for t in output]
    dbg.dassert_isinstance(output, list)
    return output


def _tee(cmd, executable, abort_on_error):
    """
    Execute command "cmd", capturing its output and removing empty lines.
    :return: list of strings
    """
    _LOG.debug("cmd=%s executable=%s", cmd, executable)
    _, output = si.system_to_string(cmd, abort_on_error=abort_on_error)
    dbg.dassert_isinstance(output, str)
    _LOG.debug("output1='\n%s'", output)
    output = output.split("\n")
    output = _remove_empty_lines(output)
    _LOG.debug("output2='\n%s'", "\n".join(output))
    return output


# #############################################################################
# Handle files.
# #############################################################################


def _get_files(args):
    """
    Return the list of files to process given the command line arguments.
    """
    file_names = []
    if args.files:
        # Files are specified.
        file_names = args.files
    else:
        if args.previous_git_commit_files is not None:
            # Get all the git in user previous commit.
            n_commits = args.previous_git_commit_files
            _LOG.info("Using %s previous commits", n_commits)
            file_names = git.get_previous_committed_files(n_commits)
        elif args.dir_name:
            if args.dir_name == "GIT_ROOT":
                dir_name = git.get_client_root()
            else:
                dir_name = args.dir_name
            dir_name = os.path.abspath(dir_name)
            _LOG.info("Looking for all files in '%s'", dir_name)
            dbg.dassert_exists(dir_name)
            cmd = "find %s -name '*' -type f" % dir_name
            _, output = si.system_to_string(cmd)
            file_names = output.split("\n")
        if not file_names or args.current_git_files:
            # Get all the git modified files.
            file_names = git.get_modified_files()
    # Keep only actual .py and .ipynb files.
    file_names = _filter_target_files(file_names)
    # Remove files.
    if args.skip_py:
        file_names = [f for f in file_names if not is_py_file(f)]
    if args.skip_ipynb:
        file_names = [f for f in file_names if not is_ipynb_file(f)]
    if args.skip_paired_jupytext:
        file_names = [f for f in file_names if not is_paired_jupytext_file(f)]
    # Keep files.
    if args.only_py:
        file_names = [
            f
            for f in file_names
            if is_py_file(f) and not is_paired_jupytext_file(f)
        ]
    if args.only_ipynb:
        file_names = [f for f in file_names if is_ipynb_file(f)]
    if args.only_paired_jupytext:
        file_names = [f for f in file_names if is_paired_jupytext_file(f)]
    #
    _LOG.debug("file_names=(%s) %s", len(file_names), " ".join(file_names))
    if not file_names:
        msg = "No files were selected"
        _LOG.error(msg)
        raise ValueError(msg)
    return file_names


# #############################################################################
# Actions.
# #############################################################################

# We use the command line instead of API because:
# - some tools don't have a public API
# - this make easier to reproduce / test commands using the command lines and
#   then incorporate in the code
# - it allows to have clear control over options


def _check_exec(tool):
    """
    :return: True if the executables "tool" can be executed.
    """
    rc = _system("which %s" % tool, abort_on_error=False)
    return rc == 0


_THIS_MODULE = sys.modules[__name__]


def _get_action_func(action):
    """
    Return the function corresponding to the passed string.
    """
    # Dynamic dispatch doesn't work with joblib since this module is injected
    # in another module.
    # func_name = "_" + action
    # dbg.dassert(
    #        hasattr(_THIS_MODULE, func_name),
    #        msg="Invalid function '%s' in '%s'" % (func_name, _THIS_MODULE))
    # return getattr(_THIS_MODULE, func_name)
    map_ = {
        "autoflake": _autoflake,
        "basic_hygiene": _basic_hygiene,
        "black": _black,
        "flake8": _flake8,
        "ipynb_format": _ipynb_format,
        "isort": _isort,
        "pydocstyle": _pydocstyle,
        "pylint": _pylint,
        "pyment": _pyment,
        "python_compile": _python_compile,
        "sync_jupytext": _sync_jupytext,
        "test_jupytext": _test_jupytext,
        "yapf": _yapf,
    }
    return map_[action]


def _remove_not_possible_actions(actions):
    """
    Check whether each action in "actions" can be executed and return a list of
    the actions that can be executed.
    :return: list of strings representing actions
    """
    actions_tmp = []
    for action in actions:
        func = _get_action_func(action)
        is_possible = func(file_name=None, pedantic=None, check_if_possible=True)
        if not is_possible:
            _LOG.warning("Can't execute action '%s': skipping", action)
        else:
            actions_tmp.append(action)
    return actions_tmp


def _actions_to_string(actions):
    actions_as_str = [
        "%16s: %s" % (a, "Yes" if a in actions else "-") for a in _ALL_ACTIONS
    ]
    return "\n".join(actions_as_str)


def _test_actions():
    _LOG.info("Testing actions")
    # Check all the actions.
    num_not_poss = 0
    possible_actions = []
    for action in _ALL_ACTIONS:
        func = _get_action_func(action)
        is_possible = func(file_name=None, pedantic=False, check_if_possible=True)
        _LOG.debug("%s -> %s", action, is_possible)
        if is_possible:
            possible_actions.append(action)
        else:
            num_not_poss += 1
    # Report results.
    actions_as_str = _actions_to_string(possible_actions)
    _LOG.info("Possible actions:\n%s", printing.space(actions_as_str))
    if num_not_poss > 0:
        _LOG.warning("There are %s actions that are not possible", num_not_poss)
    else:
        _LOG.info("All actions are possible")


# ##############################################################################

# Each action accepts:
# :param file_name: name of the file to process
# :param pendantic: True if it needs to be run in angry mode
# :param check_if_possible: check if the action can be executed on filename
# :return: list of strings representing the output


def _basic_hygiene(file_name, pedantic, check_if_possible):
    _ = pedantic
    if check_if_possible:
        # We don't need any special executable, so we can always run this action.
        return True
    output = []
    # Read file.
    dbg.dassert(file_name)
    txt = io_.from_file(file_name, split=True)
    # Process file.
    txt_new = []
    for line in txt:
        if "\t" in line:
            msg = "Found tabs in %s: please use 4 spaces as per PEP8" % file_name
            _LOG.warning(msg)
            output.append(msg)
        # Convert tabs.
        line = line.replace("\t", " " * 4)
        # Remove trailing spaces.
        line = line.rstrip()
        # dos2unix.
        line = line.replace("\r\n", "\n")
        # TODO(gp): Remove empty lines in functions.
        #
        txt_new.append(line.rstrip("\n"))
    # Remove whitespaces at the end of file.
    while txt_new and (txt_new[-1] == "\n"):
        # While the last item in the list is blank, removes last element.
        txt_new.pop(-1)
    # Write.
    txt = "\n".join(txt)
    txt_new = "\n".join(txt_new)
    if txt != txt_new:
        io_.to_file(file_name, txt_new)
    #
    return output


def _python_compile(file_name, pedantic, check_if_possible):
    """
    Check that the code is valid python.
    """
    _ = pedantic
    if check_if_possible:
        return True
    #
    dbg.dassert(file_name)
    if not is_py_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    py_compile.compile(file_name, doraise=True)
    return []


def _autoflake(file_name, pedantic, check_if_possible):
    """
    Remove unused imports and variables.
    """
    _ = pedantic
    executable = "autoflake"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Applicable to only python file.
    if not is_py_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    opts = "-i --remove-all-unused-imports --remove-unused-variables"
    cmd = executable + " %s %s" % (opts, file_name)
    _system(cmd, abort_on_error=True)
    return []


def _yapf(file_name, pedantic, check_if_possible):
    """
    Apply yapf code formatter.
    """
    _ = pedantic
    executable = "yapf"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Applicable to only python file.
    if not is_py_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    opts = "-i --style='google'"
    cmd = executable + " %s %s" % (opts, file_name)
    _system(cmd, abort_on_error=True)
    return []


def _black(file_name, pedantic, check_if_possible):
    """
    Apply black code formatter.
    """
    _ = pedantic
    executable = "black"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Applicable to only python file.
    if not is_py_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    opts = "--line-length 82"
    cmd = executable + " %s %s" % (opts, file_name)
    _system(cmd, abort_on_error=True)
    return []


def _isort(file_name, pedantic, check_if_possible):
    """
    Sort imports using isort.
    """
    _ = pedantic
    executable = "isort"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Applicable to only python file.
    if not is_py_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    cmd = executable + " %s" % file_name
    _system(cmd, abort_on_error=True)
    return []


def _flake8(file_name, pedantic, check_if_possible):
    """
    Look for formatting and semantic issues in code and docstrings.
    It relies on:
        - mccabe
        - pycodestyle
        - pyflakes
    """
    _ = pedantic
    executable = "flake8"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Applicable to only python file.
    if not is_py_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    opts = "--exit-zero --doctests --max-line-length=82 -j 4"
    disabled_checks = [
        # Because of black, disable
        #   "W503 line break before binary operator"
        "W503",
        # E265 block comment should start with '# '
        "E265",
    ]
    opts += " --ignore=" + ",".join(disabled_checks)
    cmd = executable + " %s %s" % (opts, file_name)
    return _tee(cmd, executable, abort_on_error=True)


def _pydocstyle(file_name, pedantic, check_if_possible):
    _ = pedantic
    executable = "pydocstyle"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Applicable to only python file.
    if not is_py_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    # http://www.pydocstyle.org/en/2.1.1/error_codes.html
    ignore = [
        # D200: One-line docstring should fit on one line with quotes
        "D200",
        # D202: No blank lines allowed after function docstring
        "D202",
        # D212: Multi-line docstring summary should start at the first line
        "D212",
        # D203: 1 blank line required before class docstring (found 0)
        "D203",
        # D205: 1 blank line required between summary line and description
        "D205",
        # D400: First line should end with a period (not ':')
        "D400",
    ]
    if not pedantic:
        ignore.extend(
            [
                # D100: Missing docstring in public module
                "D100",
                # D101: Missing docstring in public class
                "D101",
                # D102: Missing docstring in public method
                "D102",
                # D103: Missing docstring in public function
                "D103",
                # D104: Missing docstring in public package
                "D104",
                # D107: Missing docstring in __init__
                "D107",
                # D401: First line should be in imperative mood
                "D401",
            ]
        )
    opts = ""
    if ignore:
        opts += "--ignore " + ",".join(ignore)
    # yapf: disable
    cmd = executable + " %s %s" % (opts, file_name)
    # yapf: enable
    # We don't abort on error on pydocstyle, since it returns error if there is
    # any violation.
    _, file_lines = si.system_to_string(cmd, abort_on_error=False)
    # Process lint_log transforming:
    #   linter_v2.py:1 at module level:
    #       D400: First line should end with a period (not ':')
    # into:
    #   linter_v2.py:1: at module level: D400: First line should end with a
    #   period (not ':')
    #
    output = []
    #
    file_lines = file_lines.split("\n")
    lines = ["", ""]
    for cnt, line in enumerate(file_lines):
        line = line.rstrip("\n")
        # _log.debug("line=%s", line)
        if cnt % 2 == 0:
            regex = r"(\s(at|in)\s)"
            subst = r":\1"
            line = re.sub(regex, subst, line)
        else:
            line = line.lstrip()
        # _log.debug("-> line=%s", line)
        lines[cnt % 2] = line
        if cnt % 2 == 1:
            line = "".join(lines)
            output.append(line)
    #
    return output


def _pyment(file_name, pedantic, check_if_possible):
    _ = pedantic
    executable = "pyment"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Applicable to only python file.
    if not is_py_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    opts = "-w --first-line False -o reST"
    cmd = executable + " %s %s" % (opts, file_name)
    return _tee(cmd, executable, abort_on_error=False)


def _pylint(file_name, pedantic, check_if_possible):
    executable = "pylint"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Applicable to only python file.
    if not is_py_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    opts = ""
    # We ignore these errors as too picky.
    ignore = [
        # [C0304(missing-final-newline), ] Final newline missing
        "C0304",
        # [C0330(bad-continuation), ] Wrong hanging indentation before block
        #   (add 4 spaces).
        # Black and pylint don't agree on the formatting.
        "C0330",
        # [C0412(ungrouped-imports), ] Imports from package ... are not grouped
        "C0412",
        # [W0511(fixme), ]
        "W0511",
        # TODO(gp): Not clear what is the problem.
        # [W1113(keyword-arg-before-vararg), ] Keyword argument before variable
        # pos itional arguments list in the definition of
        "W1113",
    ]
    is_test_code = "test" in file_name.split("/")
    _LOG.debug("is_test_code=%s", is_test_code)
    if is_test_code:
        # TODO(gp): For files inside "test", disable:
        ignore.extend(
            [
                # [C0103(invalid-name), ] Class name "Test_dassert_eq1"
                #   doesn't conform to PascalCase naming style
                "C0103",
                # [R0201(no-self-use), ] Method could be a function
                "R0201",
                # [W0212(protected-access), ] Access to a protected member
                #   _get_default_tempdir of a client class
                "W0212",
            ]
        )
    is_jupytext_code = is_paired_jupytext_file(file_name)
    _LOG.debug("is_jupytext_code=%s", is_jupytext_code)
    if not pedantic:
        ignore.extend(
            [
                # [C0103(invalid-name), ] Constant name "..." doesn't conform to
                #   UPPER_CASE naming style
                "C0103",
                # [C0111(missing - docstring), ] Missing module docstring
                "C0111",
                # [C0301(line-too-long), ] Line too long (1065/100)
                "C0301",
            ]
        )
    if ignore:
        opts += "--disable " + ",".join(ignore)
    # Allow short variables, as long as camel-case.
    opts += ' --variable-rgx="[a-z0-9_]{1,30}$"'
    opts += ' --argument-rgx="[a-z0-9_]{1,30}$"'
    opts += " --ignored-modules=pandas --output-format=parseable"
    opts += " -j 4"
    cmd = executable + " %s %s" % (opts, file_name)
    return _tee(cmd, executable, abort_on_error=False)


def _ipynb_format(file_name, pedantic, check_if_possible):
    _ = pedantic
    curr_path = os.path.dirname(os.path.realpath(sys.argv[0]))
    executable = "%s/ipynb_format.py" % curr_path
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Applicable to only ipynb file.
    if not is_ipynb_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    cmd = executable + " %s" % file_name
    _system(cmd)
    return []


# ##############################################################################


# TODO(gp): Move in a more general file.
def is_py_file(file_name):
    """
    Return whether a file is a python file.
    """
    return file_name.endswith(".py")


def is_ipynb_file(file_name):
    """
    Return whether a file is a jupyter notebook file.
    """
    return file_name.endswith(".ipynb")


def from_python_to_ipynb_file(file_name):
    dbg.dassert(is_py_file(file_name))
    ret = file_name.replace(".py", ".ipynb")
    return ret


def from_ipynb_to_python_file(file_name):
    dbg.dassert(is_ipynb_file(file_name))
    ret = file_name.replace(".ipynb", ".py")
    return ret


def is_paired_jupytext_file(file_name):
    """
    Return whether a file is a paired jupytext file.
    """
    is_paired = (
        is_py_file(file_name)
        and os.path.exists(from_python_to_ipynb_file(file_name))
        or (
            is_ipynb_file(file_name)
            and os.path.exists(from_ipynb_to_python_file(file_name))
        )
    )
    return is_paired


def _sync_jupytext(file_name, pedantic, check_if_possible):
    _ = pedantic
    executable = "jupytext"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    output = []
    # Run if it's:
    # - a ipynb file without a py (i.e., not paired), or
    # - a ipynb file and paired (to avoid to run it twice)
    # so always and only a ipynb file.
    if is_py_file(file_name) and not is_paired_jupytext_file(file_name):
        # It is a python file, without a paired notebook: nothing to do.
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    if is_ipynb_file(file_name) and not is_paired_jupytext_file(file_name):
        # It is a ipynb and it is unpaired: create the python file.
        msg = (
            "There was no paired notebook for '%s': created and added to git"
            % file_name
        )
        _LOG.warning(msg)
        output.append(msg)
        # Convert a notebook into jupytext.
        cmd = []
        cmd.append(executable)
        cmd.append("--update-metadata")
        cmd.append("""{"jupytext":{"formats":"ipynb, py:percent"}}'""")
        cmd.append(file_name)
        cmd = " ".join(cmd)
        _system(cmd)
        #
        # Test the ipynb -> py:percent -> ipynb round trip conversion
        cmd = executable + " --test --stop --to py:percent %s" % file_name
        _system(cmd)
        #
        cmd = executable + " --to py:percent %s" % file_name
        _system(cmd)
        #
        py_file_name = from_ipynb_to_python_file(file_name)
        cmd = "git add %s" % py_file_name
        _system(cmd)
    elif is_paired_jupytext_file(file_name):
        cmd = executable + " --sync --update --to py:percent %s" % file_name
        _system(cmd)
    else:
        dbg.dfatal("Never get here")
    return output


def _test_jupytext(file_name, pedantic, check_if_possible):
    _ = pedantic
    executable = "jupytext"
    if check_if_possible:
        return _check_exec(executable)
    #
    dbg.dassert(file_name)
    # Run if it's:
    # - a ipynb file without a py (i.e., not paired), or
    # - a ipynb file and paired (to avoid to run it twice)
    # so always and only a ipynb file.
    if not is_ipynb_file(file_name):
        _LOG.debug("Skipping file_name='%s'", file_name)
        return []
    cmd = executable + " --test --to py:percent %s" % file_name
    _system(cmd)
    return []


# #############################################################################


def _lint(file_name, actions, pedantic, debug):
    output = []
    _LOG.info("\n%s", printing.frame(file_name, char1="="))
    for action in actions:
        _LOG.debug("\n%s", printing.frame(action, char1="-"))
        print("## %-20s (%s)" % (action, file_name))
        if debug:
            # Make a copy after each action.
            dst_file_name = file_name + "." + action
            cmd = "cp -a %s %s" % (file_name, dst_file_name)
            os.system(cmd)
        else:
            dst_file_name = file_name
        func = _get_action_func(action)
        # We want to run the stages, and not check.
        check_if_possible = False
        output_tmp = func(dst_file_name, pedantic, check_if_possible)
        # Annotate with executable [tag].
        output_tmp = _annotate_output(output_tmp, action)
        dbg.dassert_isinstance(
            output_tmp, list, msg="action=%s file_name=%s" % (action, file_name)
        )
        output.extend(output_tmp)
        if output_tmp:
            _LOG.info("\n%s", "\n".join(output_tmp))
    return output


def _select_actions(args):
    # Select phases.
    actions = args.action
    if isinstance(actions, str) and " " in actions:
        actions = actions.split(" ")
    if not actions or args.all:
        actions = _ALL_ACTIONS[:]
    if args.quick:
        actions = [a for a in _ALL_ACTIONS if a != "pylint"]
    # Validate actions.
    actions = set(actions)
    for action in actions:
        if action not in _ALL_ACTIONS:
            raise ValueError("Invalid action '%s'" % action)
    # Reorder actions according to _ALL_ACTIONS.
    actions_tmp = []
    for action in _ALL_ACTIONS:
        if action in actions:
            actions_tmp.append(action)
    actions = actions_tmp
    # Check which tools are available.
    actions = _remove_not_possible_actions(actions)
    actions_as_str = _actions_to_string(actions)
    _LOG.info("# Action selected:\n%s", printing.space(actions_as_str))
    return actions


def _run_linter(actions, args, file_names):
    num_steps = len(file_names) * len(actions)
    _LOG.info(
        "Num of files=%d, num of actions=%d -> num of steps=%d",
        len(file_names),
        len(actions),
        num_steps,
    )
    pedantic = args.pedantic
    num_threads = args.num_threads
    if len(file_names) == 1:
        num_threads = "serial"
        _LOG.warning(
            "Using num_threads='%s' since there is a single file", num_threads
        )
    if num_threads == "serial":
        output = []
        for file_name in file_names:
            output_tmp = _lint(file_name, actions, pedantic, args.debug)
            output.extend(output_tmp)
    else:
        num_threads = int(num_threads)
        # -1 is interpreted by joblib like for all cores.
        _LOG.info(
            "Using %s threads", num_threads if num_threads > 0 else "all CPUs"
        )
        from joblib import Parallel, delayed

        output_tmp = Parallel(n_jobs=num_threads, verbose=50)(
            delayed(_lint)(file_name, actions, pedantic, args.debug)
            for file_name in file_names
        )
        output = list(itertools.chain.from_iterable(output_tmp))
    output.append("# cmd line='%s'" % dbg.get_command_line())
    # TODO(gp): datetime_.get_timestamp().
    output.append("# datetime='%s'" % datetime.datetime.now())
    output = _remove_empty_lines(output)
    return output


# #############################################################################
# Main.
# #############################################################################

# Actions and if they read / write files.
# The order of this list implies the order in which they are executed.
_VALID_ACTIONS_META = [
    ("basic_hygiene", "w", "Clean up (e.g., tabs, trailing spaces)."),
    ("python_compile", "r", "Check that python code is valid"),
    (
        "autoflake",
        "w",
        "Removes unused imports and unused variables as reported by pyflakes.",
    ),
    (
        "isort",
        "w",
        "Sort Python import definitions alphabetically within logical sections.",
    ),
    # Superseded by black.
    # ("yapf", "w",
    #    "Formatter for Python code."),
    ("black", "w", "The uncompromising code formatter."),
    ("flake8", "r", "Tool For Style Guide Enforcement."),
    ("pydocstyle", "r", "Docstring style checker."),
    # Not installable through conda.
    # ("pyment", "w",
    #   "Create, update or convert docstring."),
    ("pylint", "w", "Check that module(s) satisfy a coding standard."),
    ("sync_jupytext", "w", "Create / sync jupytext files."),
    ("test_jupytext", "r", "Test jupytext files."),
    # Superseded by "sync_jupytext".
    # ("ipynb_format", "w",
    #   "Format jupyter code using yapf."),
]

_ALL_ACTIONS = list(zip(*_VALID_ACTIONS_META))[0]


def _main(args):
    dbg.init_logger(args.log_level)
    #
    if args.test_actions:
        _test_actions()
        _LOG.warning("Exiting as requested")
        sys.exit(0)
    # Select files.
    file_names = _get_files(args)
    _LOG.info(
        "# Processing %s files:\n%s",
        len(file_names),
        printing.space("\n".join(file_names)),
    )
    if args.collect_only:
        _LOG.warning("Exiting as requested")
        sys.exit(0)
    actions = _select_actions(args)
    # Create tmp dir.
    io_.create_dir(_TMP_DIR, incremental=False)
    _LOG.info("tmp_dir='%s'", _TMP_DIR)
    # Run linter.
    output = _run_linter(actions, args, file_names)
    # Print linter output.
    print(printing.frame(args.linter_log, char1="/").rstrip("\n"))
    print("\n".join(output) + "\n")
    print(printing.line(char="/").rstrip("\n"))
    # Write file.
    output = "\n".join(output)
    io_.to_file(args.linter_log, output)
    # Compute the number of lints.
    num_lints = 0
    for line in output.split("\n"):
        # dev_scripts/linter.py:493: ... [pydocstyle]
        if re.search(r"\S+:\d+.*\[\S+\]", line):
            num_lints += 1
    _LOG.info("num_lints=%d", num_lints)
    if num_lints != 0:
        _LOG.info(
            "You can quickfix the issues with\n> vim -c 'cfile %s'",
            args.linter_log,
        )
    #
    if not args.no_cleanup:
        io_.delete_dir(_TMP_DIR)
    else:
        _LOG.warning("Leaving tmp files in '%s'", _TMP_DIR)
    return num_lints


def _parse():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # Select files.
    parser.add_argument(
        "-f", "--files", nargs="+", type=str, help="Files to process"
    )
    parser.add_argument(
        "-c",
        "--current_git_files",
        action="store_true",
        help="Select all files modified in the current git client",
    )
    parser.add_argument(
        "-p",
        "--previous_git_commit_files",
        nargs="?",
        type=int,
        const=1,
        default=None,
        help="Select all files modified in previous 'n' user git commit",
    )
    parser.add_argument(
        "-d",
        "--dir_name",
        action="store",
        help="Select all files in a dir. 'GIT_ROOT' to select git root",
    )
    # Select files based on type.
    parser.add_argument(
        "--skip_py", action="store_true", help="Do not process python scripts"
    )
    parser.add_argument(
        "--skip_ipynb",
        action="store_true",
        help="Do not process jupyter notebooks",
    )
    parser.add_argument(
        "--skip_paired_jupytext",
        action="store_true",
        help="Do not process paired notebooks",
    )
    parser.add_argument(
        "--only_py",
        action="store_true",
        help="Process only python scripts " "excluding paired notebooks",
    )
    parser.add_argument(
        "--only_ipynb", action="store_true", help="Process only jupyter notebooks"
    )
    parser.add_argument(
        "--only_paired_jupytext",
        action="store_true",
        help="Process only paired notebooks",
    )
    # Debug.
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Generate one file per transformation",
    )
    parser.add_argument(
        "--no_cleanup", action="store_true", help="Do not clean up tmp files"
    )
    # Test.
    parser.add_argument(
        "--collect_only",
        action="store_true",
        help="Print only the files to process and stop",
    )
    parser.add_argument(
        "--test_actions", action="store_true", help="Print the possible actions"
    )
    # Select actions.
    parser.add_argument("--action", action="append", help="Run a specific check")
    parser.add_argument(
        "--quick", action="store_true", help="Run all quick phases"
    )
    parser.add_argument(
        "--all", action="store_true", help="Run all recommended phases"
    )
    parser.add_argument(
        "--pedantic", action="store_true", help="Run some purely cosmetic lints"
    )
    parser.add_argument(
        "--num_threads",
        action="store",
        default="-1",
        help="Number of threads to use ('serial' to run serially, -1 to use "
        "all CPUs)",
    )
    #
    parser.add_argument(
        "--linter_log",
        default="./linter_warnings.txt",
        help="File storing the warnings",
    )
    parser.add_argument(
        "-v",
        dest="log_level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level",
    )
    args = parser.parse_args()
    rc = _main(args)
    sys.exit(rc)


if __name__ == "__main__":
    _parse()
