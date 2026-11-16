VILO — JEDEN PROJEKT / WINDOWS DESKTOP 0.4.0
========================================

CO TO JEST?
To ten sam komunikator VILO z serwerami, rolami i profilami.
W tej aktualizacji naprawiono sposób uruchamiania aplikacji DESKTOP:
Electron otwiera osobne okno VILO, a lokalny backend uruchamia się
sam w tle. Nie trzeba uruchamiać CMD ani otwierać przeglądarki.

WAŻNE: TO ZIP Z KODEM ŹRÓDŁOWYM, NIE GOTOWY INSTALATOR .EXE.
Gotowy instalator musi zostać zbudowany na Windows (np. przez GitHub Actions).
Build jest skonfigurowany, ale nie został zweryfikowany na Windows w tej sesji.

JAK ZBUDOWAĆ INSTALATOR NA GITHUB (REPOZYTORIUM xMarbel/sssa)
1. Rozpakuj ZIP. W środku jest folder VILO.
2. Wgraj ZAWARTOŚĆ folderu VILO do głównego katalogu repozytorium
   na GitHub. Zachowaj układ folderów i ukryty katalog .github.
   Wygodnie zrobić to przez GitHub Desktop: sklonuj repozytorium,
   skopiuj zawartość VILO do folderu repozytorium, potem Commit/Push.
   Nie wgrywaj folderu data z kontami, wiadomościami ani hasłami!
3. Otwórz repozytorium na GitHub -> Actions.
4. Wybierz "VILO - instalator Windows" -> Run workflow -> Run workflow.
5. Gdy zakończy się bez błędów, wejdź w wynik uruchomienia i pobierz
   "VILO-Windows-Instalator" z sekcji Artifacts.
6. Rozpakuj pobrane archiwum i zobacz VILO-Setup-0.4.0.exe.
   Po instalacji skrót VILO będzie na pulpicie i w menu Start.

WINDOWS SMART APP CONTROL
Instalator NIE jest podpisany cyfrowo. Windows może go zablokować.
Nie wyłączaj Smart App Control ani innych zabezpieczeń, aby
uruchomić nieznany, niepodpisany program. Do publicznej dystrybucji
potrzebne są podpisywanie kodu i odpowiednie testy bezpieczeństwa.

GDZIE SĄ ZAPISANE DANE?
Aplikacja po instalacji używa osobnego katalogu użytkownika Windows:
%APPDATA%\vilo-desktop\local-data\ (dokładny katalog może zależeć
od nazwy aplikacji i konfiguracji systemu).
Nie usuwa automatycznie starych danych. NIE przenosi ich automatycznie.
Przed migracją należy zachować kopię starego folderu data.
Plik diagnostyczny: vilo-desktop.log w katalogu danych aplikacji.

CO JEST TERAZ IMPLEMENTOWANE?
- Konta i logowanie testowe, tekst na żywo, wiadomości prywatne.
- Grupy i serwery, kategorie, kanały tekstowe, ogłoszenia.
- Role z uprawnieniami, prywatne kanały, regulamin i powitania.
- Profile ze zdjęciem, banerem, statusem, opisem i nazwą.
- Ankiety, podstawowe boty odpowiedzi, załączniki.
- Motyw VILO Modern + Clean, motyw jasny/ciemny, personalizacja.

CZEGO JESZCZE BRAKUJE?
- Publicznego hostingu i połączenia między różnymi komputerami.
  Obecny serwer działa lokalnie, na Twoim komputerze.
- Rozmów głosowych i wideo oraz aplikacji na Android/iOS.
- Zaawansowanego Studio botów, 2FA i odzyskiwania haseł.
- Podpisanego cyfrowo instalatora i audytu bezpieczeństwa.

TESTOWANIE KODU (dla programujących)
  python -m pip install -r backend/requirements.txt httpx
  python -m unittest discover -s tests -v
  node --check main.js
  node --check backend/static/app.js
Windows GitHub Actions dodatkowo uruchamia test spakowanego backendu
przed zbudowaniem instalatora.

LOKALNY TEST W PRZEGLĄDARCE (opcjonalny, nie aplikacja desktop)
  cd backend
  py -3 -m pip install -r requirements.txt
  py -3 start.py
To osobny tryb dla programowania. Wersja desktopowa nie wymaga
CMD ani Pythona na komputerze użytkownika po zainstalowaniu.

BEZPIECZEŃSTWO
Nie udostępniaj portu VILO publicznie. Nie używaj ważnych haseł
w tej wersji testowej i nie publikuj kopii bazy danych.
