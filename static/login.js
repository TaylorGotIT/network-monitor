(function () {
  'use strict';

  var form = document.getElementById('loginForm');
  var errorBox = document.getElementById('loginError');
  var button = document.getElementById('loginBtn');

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
    button.disabled = false;
    button.textContent = '登录';
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    errorBox.hidden = true;
    button.disabled = true;
    button.textContent = '登录中…';

    fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: document.getElementById('username').value.trim(),
        password: document.getElementById('password').value
      })
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          showError((result.body && result.body.error) || '登录失败');
          return;
        }
        var next = new URLSearchParams(window.location.search).get('next') || '/';
        if (next.charAt(0) !== '/' || next.charAt(1) === '/') next = '/';
        window.location.href = next;
      })
      .catch(function (err) {
        showError('网络错误: ' + err.message);
      });
  });
})();
