Problem (Steiner-shortcut lower bound). A directed graph is a finite, loopless, simple digraph G = (V,E) with no isolated vertices. Let m = |E| ≥ 2. All edges have unit length; dist_X(u,v) is the minimum number of edges in a directed u-to-v path in X, and is ∞ if no such path exists.

A Steiner augmentation of G consists of a finite set W of new vertices, disjoint from V, and a set A of new directed edges whose endpoints lie in V ∪ W. It produces H = (V ∪ W, E ∪ A). The augmentation is reachability-preserving if, for every u,v ∈ V,

u reaches v in H if and only if u reaches v in G.

Its added size and depth are respectively

σ(H;G) = |W| + |A|

and

Δ(H;G) = max { dist_H(u,v) : u,v ∈ V and dist_G(u,v) < ∞ }.

Thus, reachability involving Steiner vertices is unrestricted, but the reachability relation induced on the original vertices must remain exactly unchanged.

Formalize the conjecture as follows:

(C) For every ε > 0, there exists M such that, for every integer m ≥ M and every directed graph G with exactly m edges, G has a reachability-preserving Steiner augmentation H satisfying

σ(H;G) ≤ m^(1+ε)  and  Δ(H;G) ≤ m^ε.

Prove that (C) is false. Equivalently, prove that there exists a constant ε₀ > 0 such that, for every integer M, there are an integer m ≥ M and an m-edge directed graph G for which every reachability-preserving Steiner augmentation H satisfies

σ(H;G) > m^(1+ε₀)  or  Δ(H;G) > m^ε₀.