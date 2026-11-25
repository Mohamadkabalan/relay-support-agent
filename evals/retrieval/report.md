Retrieval eval: 40 queries, embedder `BAAI/bge-small-en-v1.5`.
Command: `relay kb eval --queries /evals/retrieval/queries.yaml`

| Mode | Recall@1 | Recall@3 (95% CI) | MRR@5 |
|---|---|---|---|
| fts | 0.375 | 0.625 (0.47-0.76) | 0.529 |
| vector | 0.625 | 0.825 (0.68-0.91) | 0.729 |
| hybrid | 0.650 | 0.775 (0.62-0.88) | 0.731 |

Misses at 3 for fts (15):
- 'where can I see where my parcel is right now' -> tracking-a-package (rank 5)
- "typo in my street name on an order that's already on its way" -> change-shipping-address (rank 4)
- 'my headphones showed up four days after you said they would' -> late-deliveries (rank 5)
- 'can I include a birthday message with the package' -> delivery-instructions (rank >5)
- 'how long do I have to send something back' -> return-policy (rank >5)
- 'steps to send back a speaker I bought' -> start-a-return (rank >5)
- 'I mailed my return last week, when do I see the money' -> refund-timing (rank >5)
- "can I send back a gift card I didn't use" -> non-returnable-items (rank >5)
- 'is a game download code refundable' -> non-returnable-items (rank >5)
- 'can I swap the black watch for the silver one' -> exchanges (rank 4)
- "my new tablet won't turn on" -> damaged-or-defective-items (rank 4)
- 'the laptop got cheaper the day after I bought it' -> price-adjustments (rank >5)
- 'my earbuds stopped working after four months' -> warranty-claims (rank 4)
- 'do you take american express' -> payment-methods (rank 4)
- 'my wife wants to check on an order I placed for her' -> account-and-privacy (rank >5)

Misses at 3 for vector (7):
- 'is there a faster option than regular delivery' -> shipping-options (rank 4)
- 'my headphones showed up four days after you said they would' -> late-deliveries (rank >5)
- 'how long do I have to send something back' -> return-policy (rank 5)
- 'steps to send back a speaker I bought' -> start-a-return (rank >5)
- "can I send back a gift card I didn't use" -> non-returnable-items (rank 5)
- 'my earbuds stopped working after four months' -> warranty-claims (rank >5)
- 'my wife wants to check on an order I placed for her' -> account-and-privacy (rank >5)

Misses at 3 for hybrid (9):
- 'my headphones showed up four days after you said they would' -> late-deliveries (rank 4)
- 'can I include a birthday message with the package' -> delivery-instructions (rank 4)
- 'how long do I have to send something back' -> return-policy (rank >5)
- 'steps to send back a speaker I bought' -> start-a-return (rank >5)
- "can I send back a gift card I didn't use" -> non-returnable-items (rank 5)
- 'is a game download code refundable' -> non-returnable-items (rank 5)
- 'the laptop got cheaper the day after I bought it' -> price-adjustments (rank >5)
- 'my earbuds stopped working after four months' -> warranty-claims (rank >5)
- 'my wife wants to check on an order I placed for her' -> account-and-privacy (rank >5)
