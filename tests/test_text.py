from smi_agent.core.text import detect_language, hamming, sentences, simhash, skeleton, tokenize, word_tokens


def test_language_detection():
    assert detect_language("Национальный банк сохранил базовую ставку на прежнем уровне") == "ru"
    assert detect_language("Қазақстан Республикасының Ұлттық банкі базалық мөлшерлемені өзгертпеді") == "kk"
    assert detect_language("The central bank kept its base rate unchanged on Tuesday") == "en"


def test_decimals_stay_one_token():
    assert "16,5" in word_tokens("Ставка 16,5% годовых")
    assert "12.3" in word_tokens("inflation of 12.3% in August")


def test_tokenize_is_language_aware_and_drops_stopwords():
    ru = tokenize("Нацбанк сохранил базовую ставку", lang="ru")
    assert ru and all(len(t) <= 6 for t in ru)
    assert "и" not in tokenize("ставка и инфляция", lang="ru")


def test_sentence_split_respects_abbreviations_and_initials():
    text = "Об этом сообщил т.е. глава ведомства А. Б. Иванов. Решение вступит в силу с 1 января. Подробности позже."
    parts = sentences(text)
    assert len(parts) == 3
    assert parts[0].endswith("Иванов.")


def test_skeleton_matches_transliterations_across_languages():
    assert skeleton("Gates") == skeleton("Гейтс")
    assert skeleton("Tesla") == skeleton("Тесла")
    assert skeleton("Kazakhstan") == skeleton("Казахстан")


def test_simhash_is_close_for_near_duplicates_and_far_for_unrelated():
    a = tokenize("Нацбанк Казахстана сохранил базовую ставку на уровне 16,5% годовых решение принято на заседании")
    b = tokenize("Нацбанк Казахстана сохранил базовую ставку на уровне 16,5% годовых решение принято вчера")
    c = tokenize("В Алматы открыли новую станцию метро пассажиропоток ожидается 20 тысяч человек")
    assert hamming(simhash(a), simhash(b)) < hamming(simhash(a), simhash(c))
