// Sanitized minimum reproduction: unrelated vars objects precede one bootstrap object.
// The original 1.1.4 cardinality guard rejected this shape before checking loginid.
var vars = {theme: "dark"};
var vars = {unrelated: {mode: "fixture"}};
var vars = {
  loginid: "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
  set_type: "json:",
  ajaxtext: "/ajax?data="
};
