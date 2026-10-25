const { hostOf } = require('../src/client');
const { toBase64, joinWords } = require('../src/encoding');

describe('client', () => {
  it('extracts the hostname', () => {
    expect(hostOf('https://example.com:8443/path?q=1')).toBe('example.com');
  });
});

describe('encoding', () => {
  it('encodes text as base64', () => {
    expect(toBase64('reforge')).toBe('cmVmb3JnZQ==');
  });

  it('joins words', () => {
    expect(joinWords(['legacy', 'node'])).toBe('legacy node');
  });

  it('rejects non-arrays', () => {
    expect(() => joinWords('legacy')).toThrow(TypeError);
  });
});
