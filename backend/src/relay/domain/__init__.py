"""The store's business logic: the policy engine and the services that act on orders.

Everything a tool, the API or an operator does to store data goes through this package. It never
trusts its caller with identity: every service takes the acting customer from an `Actor` and
scopes its queries to that customer.
"""
