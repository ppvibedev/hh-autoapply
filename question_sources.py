"""Small local resolver: run settings/preferences, otherwise existing professional LLM."""
import json
import re
from pathlib import Path
import config

PRIVATE = Path(__file__).resolve().parent / 'private'


def _read(name):
    path = PRIVATE / name
    if PRIVATE.is_symlink() or path.is_symlink() or not path.exists(): return {}
    if path.stat().st_size > 120000: return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {}


def _norm(text):
    return re.sub(r'\s+', ' ', str(text).casefold().replace('ё','е')).strip().rstrip('.!?')


def _resume_telegram(resume_id):
    """Read only an explicit contact line from this resume's existing local cache."""
    if not re.fullmatch(r'[0-9a-f]+',resume_id or '',re.I): return None
    root=Path(config.RESUME_CACHE_DIR)
    path=root / f'{resume_id}.txt'
    if path.is_symlink() or any(p.is_symlink() for p in path.parents): return None
    try:
        with path.open(encoding='utf-8') as stream: text=stream.read(120001)
    except (OSError,UnicodeError): return None
    if len(text)>120000: return None
    values=set(re.findall(r'(?im)^\s*(?:телеграм|telegram)(?:\s+для\s+связи)?\s*:\s*(@[A-Za-z0-9_]{5,32}|https://t\.me/[A-Za-z0-9_]{5,32})\s*$',text))
    return next(iter(values)) if len(values)==1 else None


def _option(question, text, yes=False, aliases=()):
    kind=question['type']
    if kind in ('text','textarea','email','tel'): return text
    if kind=='number':
        if not re.fullmatch(r'-?\d+(?:\.\d+)?',text): raise ValueError('question_choice_invalid')
        return text
    if kind=='checkbox':
        if yes: return 'true'
        raise ValueError('question_choice_invalid')
    if kind in ('radio','select-one'):
        allowed={_norm(text), *map(_norm,aliases)}
        if yes: allowed.update(('да','согласен','готов'))
        matches=[o for o in question['options'] if _norm(o['label']) in allowed]
        if len(matches)==1: return str(matches[0]['value'])
    raise ValueError('question_choice_invalid')


def resolve(question, resume_id=''):
    """None means professional LLM; missing known fact raises before LLM."""
    text=_norm(question['question']); kind=question['type']
    # Unknown personal/formal facts never become inferred 'no'.
    if re.search(r'военн|военник|приписн|гражданств|разрешени.*работ|судим|карт.*банк|банк.*карт|медицин|диагноз|треть.*лиц|обработ.*данн|рассыл|маркетингов.*соглас|штраф|кредит|заем|оплат.*тестов',text):
        raise ValueError('personal_fact_unavailable')
    contact=next((key for key,pattern in (
        ('telegram',r'telegram|телеграм'),('email',r'e-?mail|электронн.*почт'),('phone',r'телефон')) if re.search(pattern,text)),None)
    if contact:
        # Explicit per-resume local contact only. No source text or contact enters LLM.
        profiles=_read('application_profile.json').get('resumes',{})
        profile=profiles.get(resume_id,{}) if isinstance(profiles,dict) else {}
        value=profile.get('contacts',{}).get(contact) if isinstance(profile,dict) else None
        if contact=='telegram' and not value:
            value=_resume_telegram(resume_id)
        patterns={'telegram':r'(?:@[A-Za-z0-9_]{5,32}|https://t\.me/[A-Za-z0-9_]{5,32})',
                  'email':r'[^\s@]+@[^\s@]+\.[^\s@]+','phone':r'\+?[\d ()-]{7,25}'}
        if not isinstance(value,str) or not re.fullmatch(patterns[contact],value): raise ValueError('contact_answer_unavailable')
        if kind not in ('text','textarea','email','tel'): raise ValueError('contact_answer_unavailable')
        return {'answer':value,'source':'local_contact','private_answer':True}
    if re.search(r'адрес|whatsapp|контактн',text): raise ValueError('contact_answer_unavailable')
    # Deliberately constrained topic patterns; compound professional questions stay with LLM.
    salary=re.search(r'(зарплат|доход|оплат|заработн).*?(ожидан|ожида|хот|миним|рассчитыва|жела|комфорт)|(?:ожидан|ожида|жела|миним).*?(зарплат|доход|оплат|заработн)',text)
    if salary:
        if re.search(r'net|gross|налог|час|доллар|евро|usd|eur',text): raise ValueError('question_answer_unknown')
        minimum=config.SALARY_MIN
        if minimum<=0: raise ValueError('question_answer_unknown')
        answer=str(minimum) if kind=='number' else f'От {minimum:,} ₽'.replace(',',' ')
        return {'answer':_option(question,answer,aliases=(str(minimum),)),'source':'run_salary_min'}
    if re.search(r'формат.*работ|работ.*формат',text) and not re.search(r'опыт|проект|команд|работали|раньше|предыдущ',text):
        codes=set(config.WORK_FORMAT_CODES)
        choices={frozenset({'REMOTE'}):('Удалённый формат',('удаленно','удаленка','удаленный','remote')),
                 frozenset({'ON_SITE'}):('Офис',('офисный','в офисе')),
                 frozenset({'HYBRID'}):('Гибрид',('гибридный',))}
        if frozenset(codes) not in choices: raise ValueError('question_answer_unknown')
        answer,aliases=choices[frozenset(codes)]
        return {'answer':_option(question,answer,aliases=aliases),'source':'run_work_format'}
    if re.search(r'(регион|географ).*поиск|(?:ищете|рассматриваете).*?(регион|географ)',text):
        answer='Без ограничения региона поиска' if not config.AREA else 'Регион поиска HH: '+config.AREA
        if config.AREA: raise ValueError('question_answer_unknown')  # An area ID is not a human location.
        return {'answer':_option(question,answer),'source':'run_search_region'}
    topics=[
        ('start',r'когда.*(?:приступ|выйти|выйдете)|срок.*выход|(?:готовы|сможете).*начать.*работ',False,('в течение недели',)),
        ('informal',r'общ.*[«" ]ты[»" ]|обращ.*[«" ]ты[»" ]',True,()),
        ('test',r'(?:готов|соглас|возмож).*тестов.*задан',True,()),
        ('travel',r'готов.*командиров|отнош.*командиров',False,()),
        ('employment',r'(?:формат|вариант|форм).*оформлен|оформлен.*(?:подход|предпоч)',False,()),
        ('timezone',r'часов.*пояс|разниц.*москв|московск.*врем',False,()),
        ('interview',r'когда.*собесед|готов.*собесед',False,()),
        ('references',r'(?:готов|можете|возмож).*рекомендац',True,()),
        ('resume_truth',r'информаци.*резюме.*достовер|достовер.*информаци.*резюме',True,()),
        ('verify_history',r'(?:можете|готовы).*подтверд.*(?:опыт|места работы).*резюме|(?:опыт|места работы).*резюме.*(?:подтверд|провер)',True,()),
        ('motivation',r'почему.*(?:ищете|ваканси|сменить.*компан|сменить.*работ)|причин.*(?:поиск|смен)',False,()),
    ]
    found=[row for row in topics if re.search(row[1],text)]
    if len(found)>1: raise ValueError('question_answer_unknown')
    if not found: return None
    key,_,yes,aliases=found[0]
    value=_read('application_preferences.json').get(key)
    if not isinstance(value,str) or not value: raise ValueError('question_answer_unknown')
    if key=='motivation': return {'llm_context':value,'source':'application_preference'}
    return {'answer':_option(question,value,yes,aliases),'source':'application_preference'}
