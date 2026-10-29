/** Protocol counters are decimal strings, never IEEE-754 numbers. */
export type Decimal = string

export function decimal(value: unknown): Decimal {
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]*)$/.test(value)
    || value.length > 19 || BigInt(value) > 9223372036854775807n) {
    throw new Error('Invalid protocol decimal')
  }
  return value
}

export function compareDecimal(left: Decimal, right: Decimal): -1 | 0 | 1 {
  const a = BigInt(decimal(left))
  const b = BigInt(decimal(right))
  return a < b ? -1 : a > b ? 1 : 0
}

export function incrementDecimal(value: Decimal): Decimal {
  return decimal((BigInt(decimal(value)) + 1n).toString())
}
