// The household's parents, injected by base.html from family.PARENTS. An attributed
// change needs one of them picked; an unattributed one is what makes a shared board useless.
const FM_PARENTS = window.FM_PARENTS || [];
const PICK_FIRST = 'Choose your name in the parent picker first.';
async function hhPost(url, data) {
  const who = localStorage.getItem('fm_who');
  if (!FM_PARENTS.includes(who)) throw new Error(PICK_FIRST);
  const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...data, who})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Could not save. Please try again.');
  return result;
}
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-hh-action]');
  if (!button) return;
  const card = button.closest('[data-task-key]');
  const status = card.querySelector('.hh-status');
  const controls = [...card.querySelectorAll('button')];
  controls.forEach(b => b.disabled = true);
  status.textContent = 'Saving…';
  try {
    await hhPost('/actions/change', {key:card.dataset.taskKey, action:button.dataset.hhAction,
      owner:card.querySelector('[data-owner]')?.value, until:card.querySelector('[data-until]')?.value});
    location.reload();
  } catch(error) {
    status.textContent = error.message;
    controls.forEach(b => b.disabled = false);
  }
});
document.querySelectorAll('form[data-parent-form]').forEach(form => {
  form.addEventListener('submit', event => {
    const who = localStorage.getItem('fm_who');
    if (!FM_PARENTS.includes(who)) {
      event.preventDefault();
      form.querySelector('.hh-status').textContent = PICK_FIRST;
      return;
    }
    form.querySelector('[name=who]').value = who;
    form.querySelector('.hh-status').textContent = 'Saving…';
  });
});
const reviewForm = document.getElementById('weekly-plan');
if (reviewForm) reviewForm.addEventListener('submit', async event => {
  event.preventDefault();
  const status = reviewForm.querySelector('.hh-status');
  const button = reviewForm.querySelector('button');
  status.textContent = 'Saving…'; button.disabled = true;
  try {
    await hhPost('/weekly-review', Object.fromEntries(new FormData(reviewForm)));
    status.textContent = 'Saved. Your shared plan is ready for the week.';
  } catch(error) { status.textContent = error.message; }
  finally { button.disabled = false; }
});
