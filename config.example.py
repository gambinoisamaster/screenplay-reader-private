"""Fish Audio API configuration.

Copy this file to config.py and fill in your values.
config.py is git-ignored so your key never leaves this machine.
"""

# Your API key from https://fish.audio (Account -> API keys)
FISH_API_KEY = ""

# Which Fish model generates the speech: "s1" (best), "s1-mini", "speech-1.6"

# Voices to offer in the casting UI: {display name: voice reference id}.
# Find voices at https://fish.audio/discovery — the id is the long hex string
# in the voice page URL, e.g. https://fish.audio/m/728f6ff2240d49308e8bffffe4d2b164
FISH_VOICES = {
    "Adrian": "PASTE_ADRIAN_VOICE_ID_HERE",
}

# Voice used for the Narrator by default (must be a key of FISH_VOICES).
FISH_DEFAULT_VOICE = "Adrian"
