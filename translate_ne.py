"""Nepali (Devanagari) -> English translation for table text.

Strategy (offline, no network needed):
  1. Normalise text (Devanagari digits -> ASCII, strip OCR junk).
  2. Apply phrase-level dictionary (longest phrases first).
  3. Translate remaining Devanagari tokens by exact dictionary lookup,
     then fuzzy lookup (tolerates OCR noise), then rule-based transliteration.
English text is left untouched.
"""
from __future__ import annotations

import difflib
import re

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
DEVANAGARI_LETTER_RE = re.compile(r"[ऄ-हक़-य़ॠ-ॡ]")
NEP_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# ---------------------------------------------------------------------------
# Phrase dictionary (checked before single words, longest first)
# ---------------------------------------------------------------------------
PHRASES: dict[str, str] = {
    "शनाखत भएको": "Identified",
    "शनाखत नभएको": "Unidentified",
    "सनाखत भएको": "Identified",
    "सनाखत नभएको": "Unidentified",
    "पहिचान भएको": "Identified",
    "पहिचान नभएको": "Unidentified",
    "व्यवस्थापन भएको शव": "Managed Bodies",
    "व्यवस्थापन शव": "Managed Bodies",
    "जम्मा शव": "Total Bodies",
    "कुल जम्मा": "Grand Total",
    "स्वास्थ्य विज्ञान प्रतिष्ठान": "Academy of Health Sciences",
    "स्वास्थ्या विज्ञान प्रतिष्ठान": "Academy of Health Sciences",
    "प्राथमिक स्वास्थ्य केन्द्र": "Primary Health Center",
    "स्वास्थ्य केन्द्र": "Health Center",
    "शिक्षण अस्पताल": "Teaching Hospital",
    "टिचिङ अस्पताल": "Teaching Hospital",
    "टिचिंग अस्पताल": "Teaching Hospital",
    "जिल्ला अस्पताल": "District Hospital",
    "प्रदेश अस्पताल": "Provincial Hospital",
    "प्रादेशिक अस्पताल": "Provincial Hospital",
    "प्रदेशिक अस्पताल": "Provincial Hospital",
    "प्रादेसिक अस्पताल": "Provincial Hospital",
    "नगर अस्पताल": "City Hospital",
    "मेडिकल कलेज": "Medical College",
    "मिसन अस्पताल": "Mission Hospital",
    "सामुदायिक वनको भवन": "Community Forest Building",
    "सामुदायिक वन": "Community Forest",
    "नेपाल ए.पि.एफ अस्पताल": "Nepal APF Hospital",
    "नेपाल ए.पी.एफ अस्पताल": "Nepal APF Hospital",
    "नेपाल प्रहरी अस्पताल": "Nepal Police Hospital",
    "प्रहरी विधि विज्ञान प्रयोगशाला": "Police Forensic Science Laboratory",
    "विधि विज्ञान प्रयोगशाला": "Forensic Science Laboratory",
    "रगतका नमुना संकलन": "Blood Sample Collection",
    "भारतिय नागरिक": "Indian citizen",
    "भारतीय नागरिक": "Indian citizen",
    "पृथ्वी चन्द्र": "Prithvi Chandra",
    "जिल्ला अनुसार": "By District",
    "प्रदेश अनुसार": "By Province",
    "शव शनाखत एवं व्यवस्थापन": "Body Identification and Management",
    "सम्मको तथ्याङ्क": "Data up to",
    "गाउँपालिका": "Rural Municipality",
    "नगरपालिका": "Municipality",
    "उपमहानगरपालिका": "Sub-Metropolitan City",
    "महानगरपालिका": "Metropolitan City",
    "ब.सु.पू.": "Bardaghat Susta East",
    "ब.सु.प.": "Bardaghat Susta West",
    "गो.न.पा.": "Gorkha Municipality",
    "ए.पि.एफ": "APF",
    "ए.पी.एफ": "APF",
    "सि.नं.": "S.N.",
    "सि.न.": "S.N.",
    "क्र.सं.": "S.N.",
    "क्र.स.": "S.N.",
    "नेपाली सेना": "Nepal Army",
    "सशस्त्र प्रहरी": "Armed Police",
}

# ---------------------------------------------------------------------------
# Word dictionary
# ---------------------------------------------------------------------------
WORDS: dict[str, str] = {
    # table vocabulary
    "प्रदेश": "Province", "जिल्ला": "District", "स्थान": "Location",
    "स्थानहरु": "Locations", "स्थानहरू": "Locations", "संख्या": "Number",
    "कैफियत": "Remarks", "जम्मा": "Total", "कुल": "Total", "शव": "Bodies",
    "मृत": "Dead", "मृतक": "Deceased", "व्यवस्थापन": "Management",
    "शनाखत": "Identified", "सनाखत": "Identified", "पहिचान": "Identification",
    "भएको": "", "नभएको": "Not", "रहेका": "Kept", "रहेको": "Kept",
    "विवरण": "Details", "नाम": "Name", "ठेगाना": "Address", "उमेर": "Age",
    "लिङ्ग": "Gender", "महिला": "Female", "पुरुष": "Male", "घाइते": "Injured",
    "बेपत्ता": "Missing", "उद्धार": "Rescue", "राहत": "Relief",
    "तथ्याङ्क": "Data", "तथ्यांक": "Data", "समय": "Time", "बजे": "o'clock",
    "दिउँसो": "afternoon", "बिहान": "morning", "साँझ": "evening",
    "सम्मको": "up to", "सम्म": "up to", "मिति": "Date", "अनुसार": "By",
    "एवं": "and", "तथा": "and", "र": "and", "का": "of", "को": "of", "की": "of",
    "मा": "in", "हरु": "", "हरू": "", "गरिएको": "", "गरेको": "",
    # institutions
    "अस्पताल": "Hospital", "अस्पताल,": "Hospital,", "प्रादेशिक": "Provincial",
    "प्रदेशिक": "Provincial", "प्रादेसिक": "Provincial", "गापा": "Rural Municipality",
    "गा.पा.": "Rural Municipality", "नपा": "Municipality", "न.पा.": "Municipality",
    "स्वास्थ्य": "Health", "स्वास्थ्या": "Health", "स्वास्थ": "Health",
    "विज्ञान": "Sciences", "प्रतिष्ठान": "Academy", "प्राथमिक": "Primary",
    "केन्द्र": "Center", "शिक्षण": "Teaching", "टिचिङ": "Teaching",
    "टिचिंग": "Teaching", "मेडिकल": "Medical", "कलेज": "College", "नगर": "City",
    "सामुदायिक": "Community", "वन": "Forest", "वनको": "Forest", "भवन": "Building",
    "मिसन": "Mission", "प्रहरी": "Police", "सशस्त्र": "Armed", "सेना": "Army",
    "प्रयोगशाला": "Laboratory", "विधि": "Forensic", "कार्यालय": "Office",
    "विश्वविद्यालय": "University", "विद्यालय": "School", "क्याम्पस": "Campus",
    "भारतिय": "Indian", "भारतीय": "Indian", "नागरिक": "Citizen", "पुरानो": "Old",
    "नयाँ": "New", "स्थित": "located at", "नेपाल": "Nepal", "नेपाली": "Nepali",
    "पूर्व": "East", "पश्चिम": "West", "उत्तर": "North", "दक्षिण": "South",
    "मध्य": "Central", "रगतका": "Blood", "रगत": "Blood", "नमुना": "Sample",
    "संकलन": "Collection",
    # provinces
    "कोशी": "Koshi", "कोसी": "Koshi", "मधेश": "Madhesh", "मधेस": "Madhesh",
    "बागमती": "Bagmati", "बाग्मती": "Bagmati", "गण्डकी": "Gandaki",
    "गंडकी": "Gandaki", "लुम्बिनी": "Lumbini", "लुम्बिनि": "Lumbini",
    "कर्णाली": "Karnali", "सुदूरपश्चिम": "Sudurpashchim", "सुदुरपश्चिम": "Sudurpashchim",
    # districts (all 77)
    "ताप्लेजुङ": "Taplejung", "पाँचथर": "Panchthar", "इलाम": "Ilam", "झापा": "Jhapa",
    "मोरङ": "Morang", "सुनसरी": "Sunsari", "धनकुटा": "Dhankuta", "तेह्रथुम": "Terhathum",
    "संखुवासभा": "Sankhuwasabha", "भोजपुर": "Bhojpur", "सोलुखुम्बु": "Solukhumbu",
    "ओखलढुंगा": "Okhaldhunga", "खोटाङ": "Khotang", "उदयपुर": "Udayapur",
    "सप्तरी": "Saptari", "सिराहा": "Siraha", "धनुषा": "Dhanusha", "महोत्तरी": "Mahottari",
    "सर्लाही": "Sarlahi", "रौतहट": "Rautahat", "बारा": "Bara", "पर्सा": "Parsa",
    "दोलखा": "Dolakha", "सिन्धुपाल्चोक": "Sindhupalchok", "रसुवा": "Rasuwa",
    "धादिङ": "Dhading", "धादिंग": "Dhading", "धादिङ्ग": "Dhading", "घादिङ्ग": "Dhading",
    "घादिङ": "Dhading", "नुवाकोट": "Nuwakot",
    "काठमाडौं": "Kathmandu", "काठमाण्डौ": "Kathmandu", "काठमाडौँ": "Kathmandu",
    "काठमाण्डु": "Kathmandu", "भक्तपुर": "Bhaktapur", "ललितपुर": "Lalitpur",
    "काभ्रेपलाञ्चोक": "Kavrepalanchok", "काभ्रे": "Kavre", "रामेछाप": "Ramechhap",
    "सिन्धुली": "Sindhuli", "मकवानपुर": "Makwanpur", "चितवन": "Chitwan",
    "गोरखा": "Gorkha", "मनाङ": "Manang", "मुस्ताङ": "Mustang", "म्याग्दी": "Myagdi",
    "कास्की": "Kaski", "लमजुङ": "Lamjung", "तनहुँ": "Tanahun", "तनहु": "Tanahun",
    "नवलपरासी": "Nawalparasi", "नवलपुर": "Nawalpur", "स्याङ्जा": "Syangja",
    "पर्वत": "Parbat", "बाग्लुङ": "Baglung", "रुकुम": "Rukum", "रोल्पा": "Rolpa",
    "प्युठान": "Pyuthan", "गुल्मी": "Gulmi", "अर्घाखाँची": "Arghakhanchi",
    "पाल्पा": "Palpa", "रुपन्देही": "Rupandehi", "कपिलवस्तु": "Kapilvastu",
    "दाङ": "Dang", "बाँके": "Banke", "बर्दिया": "Bardiya", "परासी": "Parasi",
    "डोल्पा": "Dolpa", "मुगु": "Mugu", "हुम्ला": "Humla", "जुम्ला": "Jumla",
    "कालिकोट": "Kalikot", "दैलेख": "Dailekh", "जाजरकोट": "Jajarkot",
    "सल्यान": "Salyan", "सुर्खेत": "Surkhet", "बाजुरा": "Bajura", "बझाङ": "Bajhang",
    "डोटी": "Doti", "अछाम": "Achham", "दार्चुला": "Darchula", "बैतडी": "Baitadi",
    "डडेलधुरा": "Dadeldhura", "कञ्चनपुर": "Kanchanpur", "कैलाली": "Kailali",
    # places / names
    "धुन्चे": "Dhunche", "उत्तरगया": "Uttargaya", "वोगटिटार": "Bogatitar",
    "बोगटिटार": "Bogatitar", "वेत्रावती": "Betrawati", "बेत्रावती": "Betrawati",
    "गोसाईकुण्ड": "Gosaikunda", "गोसाइकुण्ड": "Gosaikunda", "टिमुरे": "Timure",
    "पोखरा": "Pokhara", "भरतपुर": "Bharatpur", "मणीपाल": "Manipal", "मणिपाल": "Manipal",
    "दमौली": "Damauli", "हरमटारी": "Harmatari", "मध्यविन्दु": "Madhyabindu",
    "चोरमारा": "Chormara", "गैडाकोट": "Gaidakot", "भेडाबारी": "Bhedabari",
    "डण्डा": "Danda", "अंकुर": "Ankur", "पृथ्वी": "Prithvi", "चन्द्र": "Chandra",
    "बुटवल": "Butwal", "भीम": "Bhim", "भैरहवा": "Bhairahawa", "पिपरा": "Pipara",
    "बीरगंज": "Birgunj", "वीरगंज": "Birgunj", "हेटौडा": "Hetauda",
    "नेपालगंज": "Nepalgunj", "धनगढी": "Dhangadhi", "विराटनगर": "Biratnagar",
    "जनकपुर": "Janakpur", "धरान": "Dharan", "वीर": "Bir", "बीर": "Bir",
    "त्रिभुवन": "Tribhuvan", "पाटन": "Patan", "गंगालाल": "Gangalal",
}

# ---------------------------------------------------------------------------
# Transliteration (fallback for unknown words)
# ---------------------------------------------------------------------------
_VOWELS = {"अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri",
           "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ॲ": "a", "ऑ": "o"}
_MATRAS = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e",
           "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e"}
_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng", "च": "ch", "छ": "chh",
         "ज": "j", "झ": "jh", "ञ": "ny", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh",
         "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p",
         "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l",
         "व": "w", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
         "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f"}
_VIRAMA = "्"
_ANUSVARA = "ं"
_CHANDRABINDU = "ँ"
_VISARGA = "ः"
_NUKTA = "़"


def transliterate_word(word: str) -> str:
    out: list[str] = []
    pending = ""  # inherent vowel waiting to be emitted/replaced
    for ch in word:
        if ch in _CONS:
            out.append(pending)
            out.append(_CONS[ch])
            pending = "a"
        elif ch in _MATRAS:
            pending = _MATRAS[ch]
        elif ch in _VOWELS:
            out.append(pending)
            out.append(_VOWELS[ch])
            pending = ""
        elif ch == _VIRAMA:
            pending = ""
        elif ch in (_ANUSVARA, _CHANDRABINDU):
            out.append(pending)
            out.append("n")
            pending = ""
        elif ch == _VISARGA:
            out.append(pending)
            out.append("h")
            pending = ""
        elif ch == _NUKTA or ch == "‍" or ch == "‌":
            continue
        else:
            out.append(pending)
            out.append(ch)
            pending = ""
    # final inherent schwa is not pronounced in Nepali (e.g. चितवन -> Chitwan)
    if pending == "a" and len([c for c in word if c in _CONS]) > 1:
        pending = ""
    out.append(pending)
    text = "".join(out)
    return text[:1].upper() + text[1:] if text else text


def normalise(text: str) -> str:
    text = text.translate(NEP_DIGITS)
    text = text.replace("।", ".").replace("॥", "")
    text = re.sub(r"[|\[\]_~`^]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_PHRASE_KEYS = sorted(PHRASES, key=len, reverse=True)
_WORD_KEYS = list(WORDS)


def _lookup_word(token: str) -> str | None:
    core = token.strip(".,:;()-")
    if not core:
        return None
    if core in WORDS:
        return WORDS[core]
    if core in PHRASES:
        return PHRASES[core]
    # common OCR confusions: ङ्ग / ंग read for ङ, doubled virama sequences
    alt = core.replace("ङ्ग", "ङ").replace("ंग", "ङ").replace("््", "्")
    if alt != core and alt in WORDS:
        return WORDS[alt]
    # fuzzy: tolerate OCR noise (one or two wrong matras / characters)
    cutoff = 0.75 if len(core) >= 5 else 0.84
    candidates = difflib.get_close_matches(core, _WORD_KEYS, n=1, cutoff=cutoff)
    if candidates:
        return WORDS[candidates[0]]
    candidates = difflib.get_close_matches(core, _PHRASE_KEYS, n=1, cutoff=0.84)
    if candidates:
        return PHRASES[candidates[0]]
    return None


def translate(text: str) -> str:
    """Translate Nepali text to English; leave English untouched."""
    if text is None:
        return ""
    text = normalise(str(text))
    if not DEVANAGARI_RE.search(text):
        return text

    # Special title pattern: "X रहेका स्थानहरु" -> "X Locations"
    m = re.match(r"^(.+?)\s+रहेका\s+स्थानहर[ुू]\s*$", text)
    if m:
        return f"{translate(m.group(1))} Locations".strip()

    # Phrase replacement (longest first)
    for phrase in _PHRASE_KEYS:
        if phrase in text:
            text = text.replace(phrase, f" {PHRASES[phrase]} ")

    tokens = text.split()
    out: list[str] = []
    for tok in tokens:
        if not DEVANAGARI_RE.search(tok):
            out.append(tok)
            continue
        # keep punctuation attached to the token
        lead = re.match(r"^[(\[]*", tok).group(0)
        trail = re.search(r"[)\],:;]*$", tok).group(0)
        core = tok[len(lead):len(tok) - len(trail)] if trail else tok[len(lead):]
        # handle hyphen/slash compounds separately
        parts = re.split(r"([/-])", core)
        rendered = []
        for part in parts:
            if part in ("/", "-") or not part:
                rendered.append(part)
                continue
            if not DEVANAGARI_RE.search(part):
                rendered.append(part)
                continue
            hit = _lookup_word(part)
            if hit is None:
                hit = transliterate_word(part.strip("."))
            rendered.append(hit)
        word = "".join(rendered)
        out.append(f"{lead}{word}{trail}")

    result = " ".join(t for t in out if t)
    result = re.sub(r"\s+([,.;:)])", r"\1", result)
    result = re.sub(r"\(\s+", "(", result)
    result = re.sub(r"\s+", " ", result).strip()
    return result


def contains_nepali(text: str) -> bool:
    return bool(text) and bool(DEVANAGARI_LETTER_RE.search(str(text)))


if __name__ == "__main__":
    samples = [
        "Dead Body रहेका स्थानहरु", "सि.नं.", "प्रदेश", "जिल्ला", "स्थान", "संख्या",
        "शनाखत भएको", "शनाखत नभएको", "व्यवस्थापन शव", "जम्मा शव", "कैफियत",
        "जिल्ला अस्पताल धुन्चे रसुवा", "उत्तरगया गापा १ वोगटिटार",
        "गोसाईकुण्ड गापा २ टिमुरे", "नेपाल ए.पि.एफ अस्पताल", "टिचिङ अस्पताल काठमाण्डौ",
        "पोखरा स्वास्थ्या विज्ञान प्रतिष्ठान", "जिल्ला अस्पताल भरतपुर/पुरानो मेडिकल कलेज",
        "गो.न.पा.-6 हरमटारी स्थित NPI अस्पताल", "अंकुर सामुदायिक वनको भवन, नवलपरासी",
        "नवलपरासी (ब.सु.पू.)", "पश्चिम नवलपरासी", "१ भारतिय नागरिक पहिचान भएको",
        "लुम्बिनी प्रादेसिक अस्पताल बुटवल रुपन्देही", "जम्मा", "बागमती", "गण्डकी",
        "Kathmandu Valley", "सिन्धुपाल्चोक", "बाह्रबिसे",
    ]
    for s in samples:
        print(f"{s!r:55} -> {translate(s)!r}")
