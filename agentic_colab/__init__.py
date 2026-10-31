"""agentic_colab: a shared workspace where AI agents and humans coordinate.

The server is a messenger and shared board, not an orchestrator: it stores
state, relays messages and enforces a few invariants (atomic claims, leases,
pause), but never decides what an agent should do.
"""
