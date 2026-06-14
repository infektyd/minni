"""Tests for workspace_id normalization (G14)."""

import pytest
from config import normalize_workspace_id


def test_normalize_workspace_id_path_to_canonical():
    """Convert filesystem path to canonical form."""
    assert normalize_workspace_id("/Users/hansaxelsson/Projects/Minni") == "workspace-minni"
    assert normalize_workspace_id("/path/to/PROJECT") == "workspace-project"
    assert normalize_workspace_id("/Users/hansaxelsson/Projects/minni") == "workspace-minni"
    assert normalize_workspace_id("./minni") == "workspace-minni"
    assert normalize_workspace_id("MINNI") == "workspace-minni"


def test_normalize_workspace_id_already_canonical():
    """Preserve already-canonical form, lowercased."""
    assert normalize_workspace_id("workspace-minni") == "workspace-minni"
    assert normalize_workspace_id("workspace-Minni") == "workspace-minni"
    assert normalize_workspace_id("workspace-MINNI") == "workspace-minni"
    assert normalize_workspace_id("workspace-project") == "workspace-project"


def test_normalize_workspace_id_empty_or_none():
    """Empty/None values return empty string."""
    assert normalize_workspace_id(None) == ""
    assert normalize_workspace_id("") == ""
    assert normalize_workspace_id("   ") == ""


def test_normalize_workspace_id_trailing_slash():
    """Strip trailing slashes before extracting basename."""
    assert normalize_workspace_id("/Users/hansaxelsson/Projects/Minni/") == "workspace-minni"
    assert normalize_workspace_id("minni/") == "workspace-minni"


def test_normalize_workspace_id_mixed_case():
    """Case-insensitive normalization."""
    assert normalize_workspace_id("/MyWorkspace/MyProject") == "workspace-myproject"
    assert normalize_workspace_id("workspace-MyProject") == "workspace-myproject"
