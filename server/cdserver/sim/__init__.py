"""Simulated inbox for end-to-end tests on real Gmail.

generate  builds ~10,000 realistic emails over 5 years for one made-up person, plus
          the answer file (which cards exist and their expected status)
importer  writes them into a test Gmail inbox with messages.import (nothing is sent)
evaluate  compares a scan with the answer file
"""
