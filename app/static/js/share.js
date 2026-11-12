// Botão de compartilhar (_share.html, share_button): copia o link para a área de
// transferência e avisa num toast (toast.js).
//   <button data-share-url="https://..." data-share-message="Link copiado...">
(function () {
  // Sem a API (página fora de https, navegador antigo): o copiar do próprio documento.
  function legacyCopy(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(area);
    area.select();
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (e) { copied = false; }
    area.remove();
    return copied;
  }

  document.addEventListener('click', function (e) {
    var button = e.target.closest ? e.target.closest('[data-share-url]') : null;
    if (!button) return;
    var url = button.dataset.shareUrl;
    function done(copied) {
      if (copied) toast(button.dataset.shareMessage || 'Link copiado.', 'success');
      else toast('Não foi possível copiar o link. Copie o endereço: ' + url, 'error');
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(url).then(function () { done(true); }, function () { done(legacyCopy(url)); });
    } else {
      done(legacyCopy(url));
    }
  });
})();
