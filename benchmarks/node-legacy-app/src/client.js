const request = require('request');
const url = require('url');

function hostOf(address) {
  return url.parse(address).hostname;
}

function fetchStatus(address, callback) {
  request.get(address, (error, response) => callback(error, response && response.statusCode));
}

module.exports = { hostOf, fetchStatus };
