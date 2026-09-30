"""Shared test setup.

The package no longer loads .env.local as an import side effect — settings are
read explicitly through ``Settings.from_env()``. Tests that talk to real
providers need the environment populated before they construct anything, so do
it once here.
"""

from ai_caller.config import load_env

load_env()
