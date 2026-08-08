"""The platform's WebSocket layer.

Three modules, and the split is the same one every other feature on this platform
uses — vocabulary, one unit, the collection of units:

* :mod:`websocket.protocol` — encoding and decoding frames. Pure: it turns bytes
  into typed commands and typed events into strings, and knows nothing about
  sockets, users, or authorization.
* :mod:`websocket.connection` — one connected client. Owns its outbound queue,
  its subscriptions and their authorization grants, its duplicate-suppression
  window, and its liveness.
* :mod:`websocket.manager` — every connection at once. It is the platform's
  single :class:`~services.events.EventSubscriber`: it receives domain events
  from the dispatcher and routes them to the connections entitled to them.

**Nothing above this package imports it except the endpoint and the lifespan.**
Business modules publish to the dispatcher and know nothing of what is here,
which is the isolation ``15-real-time-synchronization.md`` asks for.
"""
