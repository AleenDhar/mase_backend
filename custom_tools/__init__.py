# Marks custom_tools as a regular package so `from custom_tools import ...`
# resolves reliably in all environments (e.g. the container image), not just
# where PEP 420 namespace-package resolution happens to work.
