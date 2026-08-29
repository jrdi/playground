# Reference solution

Complete, working implementations of every blank in `walgit/`. `make test` is
green against these.

Use them deliberately:

```bash
make peek EX=2.7   # one function
make peek EX=3     # a whole exercise
make solve         # overwrite walgit/ with all of it
make reset         # go back to the blanks
```

`.blank.py` is the script that derives the blanks from these files — it also
holds the authoritative map of blank number → function, which `scripts/peek.py`
reads.

Reading a solution after you have a failing test and a theory about why is
learning. Reading it first is not.
