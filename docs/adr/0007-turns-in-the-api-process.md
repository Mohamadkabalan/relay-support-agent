# ADR-0007: Turns run in the API process under a Redis single-flight lock

- **Status:** Accepted
- **Date:** 2026-11-26

## Context

A customer message must start a turn quickly, and the reply streams to whoever watches the conversation. At most one turn may run per conversation, because each turn appends to an append-only transcript that the next request replays byte for byte (ADR-0005). Messages can arrive while a turn runs, operator decisions arrive at any time and must resume the conversation, and a process can die mid-turn. Traffic is small: a demo and an eval suite.

## Decision

- **Turns are asyncio tasks in the API process** (`relay/agent/scheduler.py`). There is no job queue. A request that puts something in a conversation's inbox (a customer message, an approval outcome) kicks a task that runs turns until the inbox is empty.
- **A Redis lock makes it single-flight across replicas.** `SET NX PX` with a TTL longer than the 90-second turn deadline, released by compare-and-delete. A task that can't take the lock stops at once.
- **No wake-up is lost.** The holder checks the inbox again *after* releasing the lock. Input that failed to get the lock was committed before that attempt, so the holder sees it.
- **A sweep every minute** finishes orphaned turns (`running` with a free lock: their process stopped), expires approvals after 24 hours and resumes their conversations, and closes conversations idle for 30 minutes with nothing pending.
- **Events fan out through Redis pub/sub**, after being stored in Postgres, so any replica can serve any conversation's stream and a reconnect replays from the database.

## Consequences

- One process type serves HTTP and runs turns; local development and the compose stack stay simple.
- A deploy cancels running turns after a 10-second grace period. They stay `running`, and the next process's sweep recovers them: unanswered tool calls get interrupted results and the customer gets the failure apology, so the transcript stays valid. A turn that hadn't appended anything gives its inputs back to the inbox.
- Long turns hold an API worker's event loop only while they await I/O, which is nearly all of a turn.
- The upgrade path, if traffic grows, is a Redis-backed worker that calls the same `Scheduler.drain`; the lock and inbox already make that safe.
