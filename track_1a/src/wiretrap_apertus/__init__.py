"""Wiretrap for Apertus: audit what an Apertus agent does through its tools, not what it says.

The method (from Wiretrap, https://github.com/JannetEkka/wiretrap):

1. Give the model an agent's system prompt and tool schemas, but back every tool with an
   inert decoy that executes nothing and records every call.
2. Plant canary values (synthetic personal data) in what the decoys return.
3. Attack with requests that read like ordinary work.
4. Judge from the tool-call log, not the reply. A reply that refuses while a tool fired is
   "Said No, Did Yes"; a reply that claims an action no tool performed is "Said Yes, Did No".
"""

__version__ = "0.1.0"
