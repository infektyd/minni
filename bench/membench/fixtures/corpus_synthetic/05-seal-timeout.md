# Seal Timeout

The seal phase has a hard timeout of thirty seconds. If the content digest is
not confirmed by all witnesses within the timeout, the seal aborts and the edit
reverts to the proposed state.

Earlier drafts of this note said the timeout was sixty seconds; that was wrong
and has been corrected to thirty seconds.
