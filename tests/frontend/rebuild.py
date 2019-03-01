import os
import pytest
from buildstream.plugintestutils import cli

# Project directory
DATA_DIR = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    "project",
)


def strict_args(args, strict):
    if strict != "strict":
        return ['--no-strict', *args]
    return args


@pytest.mark.datafiles(DATA_DIR)
@pytest.mark.parametrize("strict", ["strict", "non-strict"])
def test_rebuild(datafiles, cli, strict):
    project = os.path.join(datafiles.dirname, datafiles.basename)
    checkout = os.path.join(cli.directory, 'checkout')

    # First build intermediate target.bst
    result = cli.run(project=project, args=strict_args(['build', 'target.bst'], strict))
    result.assert_success()

    # Modify base import
    with open(os.path.join(project, 'files', 'dev-files', 'usr', 'include', 'new.h'), "w") as f:
        f.write("#define NEW")

    # Rebuild base import and build top-level rebuild-target.bst
    # In non-strict mode, this does not rebuild intermediate target.bst,
    # which means that a weakly cached target.bst will be staged as dependency.
    result = cli.run(project=project, args=strict_args(['build', 'rebuild-target.bst'], strict))
    result.assert_success()
