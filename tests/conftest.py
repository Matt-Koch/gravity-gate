import os

# Server tests don't need fleet discovery (UDP beacons); the fleet tests turn it on.
os.environ.setdefault("GRAVITYGATE_FLEET", "0")
