# Backoff Policy

Retries in the Aurora Protocol use exponential backoff starting at two hundred
milliseconds and doubling each attempt, capped at five attempts. After the fifth
failed attempt the edit is abandoned and the editor is notified.

This policy applies only to the witness phase, not the seal phase.
