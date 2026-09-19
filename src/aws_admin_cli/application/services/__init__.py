"""Application-layer services: cross-use-case logic that isn't itself a use case.

``NetworkResolver`` lives here rather than in ``use_cases/`` because it isn't
a user-facing action with a single verb -- it's a shared building block
(translating human-friendly network references into resources) that both the
``vpc resolve`` diagnostic command AND, from Fase 5 onward, EC2 use cases
(``--subnet corp-private-1a`` instead of a raw ``subnet-...`` ID) depend on.
"""
