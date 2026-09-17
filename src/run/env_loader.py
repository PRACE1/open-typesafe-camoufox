"""Ported env loader — loads .env.local from solver root + nest root."""

from __future__ import annotations

import os

from dotenv import load_dotenv


def load_env() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    root_env = os.path.abspath(os.path.join(here, "..", "..", ".env.local"))
    nest_env = os.path.abspath(
        os.path.join(here, "..", "..", "..", "..", ".env.local")
    )
    if os.path.exists(nest_env):
        load_dotenv(nest_env)
    if os.path.exists(root_env):
        load_dotenv(root_env)
