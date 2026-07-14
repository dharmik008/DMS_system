// Caryanams — Voice Search Assistant
// Attaches a mic button to any search input using the browser's Web Speech API.
// Falls back gracefully (hides the mic button) on unsupported browsers.

(function () {
  var SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition;

  function attachVoiceSearch(inputId, micBtnId, options) {
    options = options || {};
    var input = document.getElementById(inputId);
    var micBtn = document.getElementById(micBtnId);
    if (!input || !micBtn) return;

    if (!SpeechRecognitionAPI) {
      // Voice not supported in this browser — hide the mic button entirely.
      micBtn.style.display = 'none';
      return;
    }

    var recognition = new SpeechRecognitionAPI();
    recognition.lang = options.lang || 'en-IN';
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;

    var listening = false;

    function setListening(on) {
      listening = on;
      micBtn.classList.toggle('voice-listening', on);
      micBtn.title = on ? 'Listening… click to stop' : 'Search by voice';
    }

    micBtn.addEventListener('click', function (e) {
      e.preventDefault();
      if (listening) {
        recognition.stop();
        return;
      }
      try {
        recognition.start();
        setListening(true);
      } catch (err) {
        // start() throws if called while already running — ignore
      }
    });

    recognition.onresult = function (event) {
      var transcript = event.results[0][0].transcript || '';
      input.value = transcript.trim();
      input.focus();

      // Fire a native 'input' event so any oninput="..." live-filter
      // handlers already on the field (e.g. filterTable(this, 'someTable'))
      // react to the voice-filled value exactly like manual typing would.
      try {
        input.dispatchEvent(new Event('input', { bubbles: true }));
      } catch (e) {
        // Older browsers without the Event constructor — ignore, the
        // autoSubmitForm path below still works for form-based searches.
      }

      if (options.autoSubmitForm) {
        var form = document.getElementById(options.autoSubmitForm);
        if (form) form.submit();
      }
      if (typeof options.onResult === 'function') {
        options.onResult(input.value);
      }
    };

    recognition.onerror = function () {
      setListening(false);
    };

    recognition.onend = function () {
      setListening(false);
    };
  }

  window.attachVoiceSearch = attachVoiceSearch;
})();
