# LisPy Conformance Wire

`lispy-conformance@2` describes language-neutral test cases. Every expectation
contains exact language stdout and exactly one outcome.

Values use `lispy-value@1` tags:

| Tag | Payload |
|---|---|
| `nil` | none |
| `boolean` | JSON boolean `value` |
| `integer` | canonical decimal string `value` |
| `float64` | 16 lowercase IEEE-754 hexadecimal `bits` |
| `string`, `symbol` | text `value` |
| `list` | tagged `items` |
| `pair` | tagged `car` and `cdr` |
| `map` | canonically ordered tagged key/value `entries` |

Portable error expectations assert only `category`; implementation type names
and diagnostic messages are non-normative.

A conforming adapter must capture language output, execute every case twice,
produce identical outcomes, reject non-finite/cyclic/unsupported values, and
emit no protocol or process stdout outside the reported `stdout` field.
