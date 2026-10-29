import { describe, expect, it } from 'vitest'
import { compareDecimal, decimal, incrementDecimal } from '../../../src/domain/decimal'

const maximum = '9223372036854775807'

describe('protocol decimal strings', () => {
  it('preserves canonical strings through the signed PostgreSQL BIGINT maximum', () => {
    for (const value of ['0', '1', '9007199254740991', '9007199254740992', '9007199254740993', maximum]) {
      expect(decimal(value)).toBe(value)
      expect(typeof decimal(value)).toBe('string')
    }
  })

  it('rejects numbers, noncanonical syntax and every out-of-range counter', () => {
    for (const value of [
      0, 1, 9007199254740992, 1n, null, undefined, {}, [], true,
      '', '00', '01', '-1', '-0', '+1', ' 1', '1 ', '1\n', '1.0', '1e2', '0x10', '１',
      '9223372036854775808', '9999999999999999999', '10000000000000000000',
      '1'.repeat(1000),
    ]) expect(() => decimal(value)).toThrow()
  })

  it('compares numerically without rounding beyond Number safe integer precision', () => {
    expect(compareDecimal('2', '10')).toBe(-1)
    expect(compareDecimal('9007199254740992', '9007199254740993')).toBe(-1)
    expect(compareDecimal('9007199254740993', '9007199254740992')).toBe(1)
    expect(compareDecimal(maximum, maximum)).toBe(0)
    expect(compareDecimal('0', maximum)).toBe(-1)
  })

  it('validates both comparison operands before ordering', () => {
    for (const value of ['01', '-1', '9223372036854775808']) {
      expect(() => compareDecimal(value, '0')).toThrow()
      expect(() => compareDecimal('0', value)).toThrow()
    }
  })

  it('increments exactly and fails closed rather than overflowing BIGINT', () => {
    expect(incrementDecimal('0')).toBe('1')
    expect(incrementDecimal('9007199254740992')).toBe('9007199254740993')
    expect(incrementDecimal('9223372036854775806')).toBe(maximum)
    expect(() => incrementDecimal(maximum)).toThrow()
    expect(() => incrementDecimal('01')).toThrow()
    expect(() => incrementDecimal('9223372036854775808')).toThrow()
  })
})
