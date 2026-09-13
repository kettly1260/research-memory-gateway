"""Repository-local test package.

Keeping ``tests`` as an explicit package prevents fresh CI environments from
resolving cross-test helper imports against an unrelated third-party package
named ``tests``.
"""
