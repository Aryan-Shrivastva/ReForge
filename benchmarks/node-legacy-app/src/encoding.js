const util = require('util');

function toBase64(text) {
  return new Buffer(text, 'utf8').toString('base64');
}

function joinWords(words) {
  if (!util.isArray(words)) {
    throw new TypeError('words must be an array');
  }
  return words.join(' ');
}

module.exports = { toBase64, joinWords };
