"""Load and save the Signal tab's state as JSON. No Qt here; the UI supplies the folder."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .engine import Profile, TraderState

VERSION = 1
FILE_NAME = 'signal_state.json'


@dataclass
class SignalDoc:
    symbol: str
    profile: Profile
    trader: TraderState = field(default_factory=TraderState)
    csv_path: Optional[str] = None       # set when prices come from a CSV instead of Yahoo
    follow_rule: str = 'manual'          # how later trades are chosen: same | trend | chase | random_one | manual
    trader_number: int = 1               # trader number for the random rule (same numbering as the Report)
    first_direction: Optional[int] = None   # side of the very first trade, set when it is opened

    def follow(self) -> Optional[dict]:
        """Settings for model-led re-entry, or None when the user chooses every entry."""
        if self.follow_rule == 'manual' or self.first_direction is None:
            return None
        return dict(rule=self.follow_rule, first_direction=self.first_direction, trader=self.trader_number)

    def to_dict(self) -> dict:
        return dict(version=VERSION, symbol=self.symbol, csv_path=self.csv_path, follow_rule=self.follow_rule,
                    trader_number=self.trader_number, first_direction=self.first_direction,
                    profile=self.profile.to_dict(), trader=self.trader.to_dict())

    @staticmethod
    def from_dict(d: dict) -> 'SignalDoc':
        return SignalDoc(symbol=d['symbol'], csv_path=d.get('csv_path'), follow_rule=d.get('follow_rule', 'manual'),
                         trader_number=d.get('trader_number', 1), first_direction=d.get('first_direction'),
                         profile=Profile.from_dict(d['profile']), trader=TraderState.from_dict(d['trader']))


def new_doc(start_account: float, symbol: str, profile: Profile, csv_path: Optional[str] = None,
            follow_rule: str = 'manual', trader_number: int = 1) -> SignalDoc:
    if not start_account > 0:
        raise ValueError('Starting account must be more than zero.')
    return SignalDoc(symbol=symbol, profile=profile, csv_path=csv_path, follow_rule=follow_rule, trader_number=trader_number,
                     trader=TraderState(start=float(start_account), account=float(start_account)))


def state_path(folder: str) -> str:
    return os.path.join(folder, FILE_NAME)


def save(folder: str, doc: SignalDoc) -> None:
    os.makedirs(folder, exist_ok=True)
    path = state_path(folder)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(doc.to_dict(), fh, indent=1)
    os.replace(tmp, path)                # atomic: a crash never leaves a half-written state file


def load(folder: str) -> Optional[SignalDoc]:
    """Return the saved state, or None when there is none. Raises ValueError if the file is unreadable."""
    path = state_path(folder)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            return SignalDoc.from_dict(json.load(fh))
    except (OSError, ValueError, KeyError, TypeError) as ex:
        raise ValueError(f'The saved state file could not be read ({path}): {ex}') from ex


def reset(folder: str) -> None:
    path = state_path(folder)
    if os.path.exists(path):
        os.remove(path)
