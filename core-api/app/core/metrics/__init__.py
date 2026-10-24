"""Metrics, pushed over OTLP (there is no scrape endpoint).

Every metric is `drills.<service_name>.<namespace>[.<sub_namespace>...].<name>`: dots separate namespaces,
each segment is snake_case, no `_total` or unit suffix (the backend adds them). Labels are closed sets
(enums) only, never tenant, project or user ids.
To add one: define it in the module for its namespace group (a new group is a new module here) through
the `counter` / `histogram` helpers in base.py, which enforce the name; import it where the event happens.
"""
