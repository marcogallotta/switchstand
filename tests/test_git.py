import subprocess

import pytest

from switchstand.git import GitError, reconcile


def test_reconcile_requires_exact_current_two_parent_tree_identity(monkeypatch, tmp_path):
    answer = lambda sha, tree, parents="": subprocess.CompletedProcess(
        (), 0, f"{sha}\n{tree}\n{parents}\n", "")
    candidate, base, merged, current, tree = (character * 40 for character in "abcde")
    replies = {
        candidate: answer(candidate, tree), base: answer(base, "f" * 40),
        merged: answer(merged, tree, f"{base} {candidate}"),
        current: answer(current, "wrong", f"{base} {candidate}"),
    }
    monkeypatch.setattr(subprocess, "run", lambda command, **options: replies[command[-1]])
    assert reconcile(tmp_path, candidate, base, merged, merged).tree == tree
    for arguments in ((candidate, base, merged, current), (candidate, base, current, current),
                      (candidate, base, base, base), (candidate, base, "HEAD", merged)):
        with pytest.raises(GitError):
            reconcile(tmp_path, *arguments)
