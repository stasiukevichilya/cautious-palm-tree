"""Source adapters: kufar and onliner."""
from .kufar import KufarAdapter
from .onliner import OnlinerAdapter

ADAPTERS = {"kufar": KufarAdapter, "onliner": OnlinerAdapter}
