# Reasoning fixture matrix

The GHA identifiers in this tree are evaluation labels only. Product code must never read this
directory, an `oracle.json`, or a GHA identifier. Each oracle is loaded only after deterministic
fact extraction so that it cannot leak an answer to the Agent.

Variants use these meanings:

- `supported`: the maximally exposed local variant. Its oracle is `SUPPORTED` when every decisive
  edge is local, but remains `CANDIDATE` for supply-chain compromise, cloud trust, runner reuse or
  weak-condition authorization that cannot honestly be proven from repository Evidence.
- `candidate`: at least one required relation is `UNKNOWN` and names missing Evidence.
- `rejected`: a concrete guard negates an edge or no sensitive sink is reachable.
