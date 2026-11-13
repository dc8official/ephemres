// What the topology map shows under the shared endpoint filter (v3.3.6 build plan 4.3, item 8).
// Which endpoints match comes from the server (GET /endpoints/match); this only works out the
// paths to them on the graph the map already has.
//
//   - Endpoints the filter matches: shown as usual.
//   - Everything on the path from the root to one of them (transit routers, subnet hubs, other
//     endpoints the traffic passes): "path" nodes.
//   - Everything else: hidden.
//
// Two ways to show path nodes, for Kenneth to compare on real data:
//   'dim'  every path node is shown faded.
//   'hide' path endpoints that don't match are left out, and their links are joined over the
//          gap (a dashed "bridged" edge); transit routers, hubs and the root stay, unfaded,
//          because they are the route itself and carry no site of their own.

function endpointIdOf(node) {
  if (node.endpoint_id) return String(node.endpoint_id)
  const type = node.node_type || node.type
  return type === 'monitored' ? String(node.id) : null
}

function isEndpointNode(node) {
  return endpointIdOf(node) !== null
}

/**
 * @param {Array} nodes   graph nodes from GET /topology ({ id, node_type, endpoint_id, ... })
 * @param {Array} edges   graph edges ({ source, target, ... })
 * @param {Set|null} matchIds  endpoint ids in the filter; null means no filter
 * @param {'dim'|'hide'} mode
 * @returns {{ nodes: Array, edges: Array }} nodes carry filterRole 'match' | 'path' when filtered;
 *   bridged edges carry bridged: true
 */
export function applyTopologyFilter(nodes, edges, matchIds, mode = 'dim') {
  if (!matchIds) return { nodes, edges }

  const byId = new Map(nodes.map((n) => [String(n.id), n]))
  const parents = new Map()
  // Paths follow the links in use; the faded normal-path links (normal_only) only show where
  // both ends are shown anyway.
  for (const e of edges.filter((x) => !x.normal_only)) {
    const t = String(e.target)
    if (!parents.has(t)) parents.set(t, [])
    parents.get(t).push(String(e.source))
  }

  const matched = new Set(
    nodes.filter((n) => {
      const epId = endpointIdOf(n)
      return epId !== null && matchIds.has(epId)
    }).map((n) => String(n.id))
  )

  // Every ancestor of a matching node is on a path to it.
  const onPath = new Set()
  const queue = [...matched]
  while (queue.length) {
    const id = queue.shift()
    for (const p of parents.get(id) || []) {
      if (!matched.has(p) && !onPath.has(p)) {
        onPath.add(p)
        queue.push(p)
      }
    }
  }

  const shown = new Set()
  for (const id of matched) shown.add(id)
  for (const id of onPath) {
    const node = byId.get(id)
    if (!node) continue
    if (mode === 'hide' && isEndpointNode(node)) continue
    shown.add(id)
  }
  // The root anchors the layout even when nothing matches.
  for (const n of nodes) {
    if ((n.node_type || n.type) === 'root') shown.add(String(n.id))
  }

  const outNodes = nodes
    .filter((n) => shown.has(String(n.id)))
    .map((n) => ({
      ...n,
      filterRole: matched.has(String(n.id)) ? 'match' : (mode === 'dim' && onPath.has(String(n.id)) ? 'path' : 'match'),
    }))

  const normalOnly = edges.filter((e) => e.normal_only && shown.has(String(e.source)) && shown.has(String(e.target)))
  edges = edges.filter((e) => !e.normal_only)
  let outEdges
  if (mode === 'dim') {
    outEdges = edges.filter((e) => shown.has(String(e.source)) && shown.has(String(e.target)))
  } else {
    // Join each shown node to its nearest shown ancestors, walking up through hidden nodes.
    const seen = new Set()
    outEdges = []
    for (const e of edges) {
      const target = String(e.target)
      if (!shown.has(target)) continue
      const stack = [{ id: String(e.source), bridged: false }]
      const visited = new Set()
      while (stack.length) {
        const { id, bridged } = stack.pop()
        if (visited.has(id)) continue
        visited.add(id)
        if (shown.has(id)) {
          const key = `${id}->${target}`
          if (!seen.has(key)) {
            seen.add(key)
            outEdges.push(bridged ? { source: id, target, bridged: true } : { ...e, source: id, target })
          }
          continue
        }
        for (const p of parents.get(id) || []) stack.push({ id: p, bridged: true })
      }
    }
  }

  return { nodes: outNodes, edges: [...outEdges, ...normalOnly] }
}
