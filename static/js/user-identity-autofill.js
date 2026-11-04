/*
 * Live Employee ID / Email suggestion, plus a live NID rehire check, for the
 * "Create user" form (Create only — never wired up on Edit, where
 * overwriting a real employee's identity from a name edit would be actively
 * harmful). Username isn't on this form at all — the server sets it equal
 * to Email on save (accounts/forms.py::CustomUserCreationForm.save()).
 *
 * Employee ID is fetched once on load. Email is fetched (debounced) once
 * First + Last Name are both filled, and re-fetched on Department change
 * since the generation convention is department-sensitive. When the name
 * space is exhausted (an unusual collision — see accounts/upload.py's
 * generate_email), the server can't suggest anything; a hint is shown next
 * to Email instead of leaving it silently blank.
 *
 * A suggested value never overwrites something HR typed by hand: each
 * field is only filled when it's still empty or still holds exactly the
 * value this script last wrote there.
 *
 * The NID field gets a separate, independent live check against
 * accounts.services.check_prior_employment — the same soft-warning logic
 * UserCreateView.form_valid runs on submit, just surfaced earlier so it
 * isn't hidden behind a blocked-on-Email situation.
 */
(function () {
    function ensureNote(field, cls) {
        var note = field.parentElement.querySelector('.js-autofill-note');
        if (!note) {
            note = document.createElement('p');
            note.className = 'js-autofill-note mt-1 text-xs ' + cls;
            field.insertAdjacentElement('afterend', note);
        }
        return note;
    }

    function init(root) {
        var first = root.querySelector('#id_first_name');
        var last = root.querySelector('#id_last_name');
        var dept = root.querySelector('#id_department');
        var email = root.querySelector('#id_email');
        var employeeId = root.querySelector('#id_employee_id');
        var nid = root.querySelector('#id_nid_number');

        var lastAuto = { employee_id: '', email: '' };
        var timer = null;

        function fillIfUntouched(field, key, value) {
            if (!value) return;
            if (field.value === '' || field.value === lastAuto[key]) {
                field.value = value;
                lastAuto[key] = value;
            }
        }

        async function refreshIdentity() {
            var params = new URLSearchParams({
                first_name: first.value.trim(),
                last_name: last.value.trim(),
                department: dept ? dept.value : '',
            });
            var r = await fetch('/users/ajax/suggest-identity/?' + params.toString(),
                                 { credentials: 'same-origin' });
            if (!r.ok) return;
            var j = await r.json();
            fillIfUntouched(employeeId, 'employee_id', j.employee_id);
            var emailNote = ensureNote(email, 'text-amber-600 dark:text-amber-400');
            if (j.email) {
                fillIfUntouched(email, 'email', j.email);
                emailNote.textContent = '';
            } else if (j.email_exhausted) {
                emailNote.textContent = "Couldn't auto-suggest an email for this name — enter one manually.";
            }
        }

        function debouncedRefreshIdentity() {
            clearTimeout(timer);
            timer = setTimeout(refreshIdentity, 400);
        }

        if (first && last && email && employeeId) {
            first.addEventListener('input', debouncedRefreshIdentity);
            last.addEventListener('input', debouncedRefreshIdentity);
            if (dept) dept.addEventListener('change', refreshIdentity);
            refreshIdentity();
        }

        if (nid) {
            var nidTimer = null;

            async function refreshNidWarning() {
                var value = nid.value.trim();
                var note = ensureNote(nid, 'text-amber-700 dark:text-amber-300 font-medium');
                if (!value) {
                    note.textContent = '';
                    return;
                }
                var params = new URLSearchParams({ nid_number: value });
                var r = await fetch('/users/ajax/check-nid/?' + params.toString(),
                                     { credentials: 'same-origin' });
                if (!r.ok) return;
                var j = await r.json();
                note.textContent = j.warning || '';
            }

            function debouncedRefreshNid() {
                clearTimeout(nidTimer);
                nidTimer = setTimeout(refreshNidWarning, 400);
            }

            nid.addEventListener('input', debouncedRefreshNid);
            nid.addEventListener('blur', refreshNidWarning);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        var form = document.querySelector('form[data-identity-autofill]');
        if (form) init(form);
    });
})();
