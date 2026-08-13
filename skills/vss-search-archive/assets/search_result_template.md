# Video Search Results

Use one block per hit. Do not compress hits into a table that can omit a field.

```text
### Hit <number>
- Source: <exact source>
- Time: <exact start> to <exact end>
- Similarity: <exact score>
- Media URL: <complete exact screenshot_url>
- Verification: <confirmed, rejected, or unverified>
- Criteria: <boolean criteria when present>
```

Every displayed hit must include every line above except `Criteria`, which is
conditional. Print the complete `screenshot_url` literally on its `Media URL:`
line; validating a URL in a tool call does not report it to the user. Before
sending the reply, require the number of `Media URL:` lines to equal the number
of successfully validated hits. Do not include raw JSON.

Similarity scores are retrieval evidence; the separate verification result
records whether the bounded clip satisfied the visual request.

Include the following section only when the nonempty displayed result set is
entirely `unverified`. Omit it if any displayed hit is `confirmed` or
`rejected`.

## Verification Step

Would you like me to verify the unverified search results?
