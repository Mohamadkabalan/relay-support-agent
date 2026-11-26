"""Input and output checks (PLAN.md §5.12).

They are for monitoring and for the model's benefit, not the main defense: the model holds no
authority (ADR-0003), so an injection that gets past these checks still can't change whose data
the tools touch or what the policy allows.
"""
