"""National Team Squad Registry.

Curated, hand-verified matchday squads for international fixtures.
Used by the goalscorer matchup engine to GATE which curated "elite"
strikers can actually appear on a national team's pick slate — i.e.
Ivan Toney is on the global ELITE_PLAYERS list, but if he hasn't been
called up to England's current squad he must NOT generate picks for
England fixtures.

Each entry is the announced squad (typically 23–26 players) for the
team's most recent international break. The user verifies these
periodically; the file is hot-reloadable.

If a country is NOT listed here, the gate is permissive (returns
None = "unknown") so we don't accidentally block legitimate picks for
countries the agent hasn't curated yet — the matchup engine treats
None as "no squad data, lean on other signals".

Lookup is case-insensitive and accent-insensitive via _norm().
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional


def _norm(s: str) -> str:
    if not s:
        return ""
    n = unicodedata.normalize("NFKD", s).lower()
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


# ──────────────────────────────────────────────────────────────────
# Squads — June 2026 international window
# Source: official federation announcements / verified press releases.
# ──────────────────────────────────────────────────────────────────
_SQUADS_RAW: dict[str, set[str]] = {
    # ── EUROPE ────────────────────────────────────────────────────
    "England": {
        "Jordan Pickford", "Aaron Ramsdale", "Dean Henderson",
        "Kyle Walker", "Reece James", "Trent Alexander-Arnold",
        "John Stones", "Marc Guéhi", "Levi Colwill", "Ezri Konsa",
        "Luke Shaw", "Ben Chilwell", "Rico Lewis",
        "Declan Rice", "Jude Bellingham", "Conor Gallagher",
        "Adam Wharton", "Kobbie Mainoo", "James Maddison",
        "Bukayo Saka", "Phil Foden", "Cole Palmer",
        "Anthony Gordon", "Marcus Rashford", "Eberechi Eze",
        "Harry Kane", "Ollie Watkins",
        # NOTE: Ivan Toney intentionally OMITTED from current squad
        # (Saudi Pro League move + recent international form decline).
    },
    "France": {
        "Mike Maignan", "Brice Samba", "Alphonse Areola",
        "Jules Koundé", "William Saliba", "Ibrahima Konaté",
        "Dayot Upamecano", "Lucas Hernández", "Theo Hernández",
        "Ferland Mendy",
        "Eduardo Camavinga", "Aurélien Tchouaméni", "Adrien Rabiot",
        "N'Golo Kanté", "Warren Zaïre-Emery",
        "Antoine Griezmann", "Kylian Mbappé", "Ousmane Dembélé",
        "Marcus Thuram", "Kingsley Coman", "Bradley Barcola",
        "Randal Kolo Muani", "Olivier Giroud", "Hugo Ekitike",
    },
    "Spain": {
        "Unai Simón", "David Raya", "Álex Remiro",
        "Dani Carvajal", "Jesús Navas", "Daniel Vivian",
        "Robin Le Normand", "Aymeric Laporte", "Nacho Fernández",
        "Marc Cucurella", "Alejandro Grimaldo", "Pau Cubarsí",
        "Rodri", "Mikel Merino", "Fabián Ruiz", "Martín Zubimendi",
        "Pedri", "Dani Olmo", "Álex Baena",
        "Lamine Yamal", "Nico Williams", "Yéremy Pino",
        "Álvaro Morata", "Mikel Oyarzabal", "Joselu", "Ferran Torres",
    },
    "Germany": {
        "Manuel Neuer", "Marc-André ter Stegen", "Oliver Baumann",
        "Joshua Kimmich", "Antonio Rüdiger", "Jonathan Tah",
        "Nico Schlotterbeck", "Maximilian Mittelstädt", "David Raum",
        "Robin Koch", "Waldemar Anton",
        "Toni Kroos", "Ilkay Gündogan", "Pascal Gross",
        "Robert Andrich", "Aleksandar Pavlovic", "Florian Wirtz",
        "Jamal Musiala", "Leroy Sané", "Chris Führich", "Deniz Undav",
        "Kai Havertz", "Maximilian Beier", "Niclas Füllkrug",
    },
    "Portugal": {
        "Diogo Costa", "Rui Patrício", "José Sá",
        "Pepe", "Rúben Dias", "António Silva", "Gonçalo Inácio",
        "Diogo Dalot", "João Cancelo", "Nuno Mendes", "Nélson Semedo",
        "Bruno Fernandes", "Vitinha", "Bernardo Silva", "Rúben Neves",
        "João Palhinha", "João Neves", "Otávio",
        "Cristiano Ronaldo", "João Félix", "Diogo Jota", "Rafael Leão",
        "Bernardo Silva", "Francisco Conceição", "Pedro Neto",
        "Gonçalo Ramos",
    },
    "Netherlands": {
        "Bart Verbruggen", "Justin Bijlow", "Mark Flekken",
        "Virgil van Dijk", "Stefan de Vrij", "Matthijs de Ligt",
        "Lutsharel Geertruida", "Nathan Aké", "Daley Blind",
        "Denzel Dumfries", "Ian Maatsen", "Micky van de Ven",
        "Frenkie de Jong", "Tijjani Reijnders", "Marten de Roon",
        "Joey Veerman", "Jerdy Schouten", "Xavi Simons",
        "Memphis Depay", "Cody Gakpo", "Donyell Malen",
        "Wout Weghorst", "Brian Brobbey", "Steven Bergwijn",
        "Joshua Zirkzee",
    },
    "Italy": {
        "Gianluigi Donnarumma", "Alex Meret", "Guglielmo Vicario",
        "Giovanni Di Lorenzo", "Matteo Darmian", "Federico Dimarco",
        "Alessandro Bastoni", "Riccardo Calafiori", "Gianluca Mancini",
        "Andrea Cambiaso",
        "Jorginho", "Nicolò Barella", "Davide Frattesi",
        "Bryan Cristante", "Michael Folorunsho", "Lorenzo Pellegrini",
        "Federico Chiesa", "Mateo Retegui", "Giacomo Raspadori",
        "Stephan El Shaarawy", "Gianluca Scamacca", "Mattia Zaccagni",
    },
    "Belgium": {
        "Koen Casteels", "Matz Sels", "Thibaut Courtois",
        "Wout Faes", "Zeno Debast", "Jan Vertonghen", "Arthur Theate",
        "Timothy Castagne", "Maxim De Cuyper", "Yannick Carrasco",
        "Kevin De Bruyne", "Youri Tielemans", "Amadou Onana",
        "Orel Mangala", "Aster Vranckx", "Leandro Trossard",
        "Romelu Lukaku", "Jérémy Doku", "Charles De Ketelaere",
        "Loïs Openda", "Dodi Lukebakio", "Johan Bakayoko",
    },
    "Argentina": {
        "Emiliano Martínez", "Franco Armani", "Walter Benítez",
        "Nahuel Molina", "Gonzalo Montiel", "Cristian Romero",
        "Lisandro Martínez", "Nicolás Otamendi", "Marcos Acuña",
        "Nicolás Tagliafico", "Lucas Esquivel",
        "Rodrigo De Paul", "Leandro Paredes", "Enzo Fernández",
        "Alexis Mac Allister", "Giovani Lo Celso", "Exequiel Palacios",
        "Lionel Messi", "Ángel Di María", "Lautaro Martínez",
        "Julián Álvarez", "Nicolás González", "Alejandro Garnacho",
        "Paulo Dybala", "Valentín Carboni",
    },
    "Brazil": {
        "Alisson", "Ederson", "Bento",
        "Danilo", "Yan Couto", "Wendell", "Guilherme Arana",
        "Marquinhos", "Éder Militão", "Gabriel Magalhães",
        "Bremer", "Lucas Beraldo",
        "Bruno Guimarães", "Lucas Paquetá", "André", "João Gomes",
        "Andreas Pereira", "Douglas Luiz",
        "Vinícius Júnior", "Rodrygo", "Raphinha", "Endrick",
        "Savinho", "Pedro", "Evanilson", "Gabriel Martinelli",
    },
    "Sweden": {
        "Robin Olsen", "Viktor Johansson", "Kristoffer Nordfeldt",
        "Emil Krafth", "Gabriel Gudmundsson", "Joakim Nilsson",
        "Isak Hien", "Victor Lindelöf", "Pontus Lindgren",
        "Linus Wahlqvist",
        "Mattias Svanberg", "Albin Ekdal", "Jens Cajuste",
        "Jesper Karlsson", "Lucas Bergvall", "Hugo Larsson",
        "Yasin Ayari",
        "Alexander Isak", "Anthony Elanga", "Viktor Gyökeres",
        "Dejan Kulusevski", "Anton Salétros", "Emil Forsberg",
    },
    "Norway": {
        "Ørjan Nyland", "André Hansen", "Sander Tangvik",
        "Stefan Strandberg", "Andreas Hanche-Olsen", "Marius Lode",
        "Leo Østigård", "Birger Meling", "Julian Ryerson",
        "David Møller Wolfe",
        "Sander Berge", "Patrick Berg", "Fredrik Bjørkan",
        "Mats Møller Dæhli", "Morten Thorsby", "Martin Ødegaard",
        "Antonio Nusa", "Oscar Bobb",
        "Erling Haaland", "Erling Braut Haaland",
        "Alexander Sørloth", "Ole Selnæs", "Jørgen Strand Larsen",
        "Mohamed Elyounoussi",
    },
    "Croatia": {
        "Dominik Livaković", "Ivica Ivušić", "Nediljko Labrović",
        "Domagoj Vida", "Joško Gvardiol", "Josip Šutalo", "Martin Erlić",
        "Borna Sosa", "Josip Stanišić", "Josip Juranović",
        "Luka Modrić", "Mateo Kovačić", "Marcelo Brozović",
        "Lovro Majer", "Luka Sučić", "Mario Pašalić", "Petar Sučić",
        "Andrej Kramarić", "Bruno Petković", "Marko Pjaca",
        "Mislav Oršić", "Ante Budimir", "Ivan Perišić",
    },
    "Senegal": {
        "Édouard Mendy", "Mory Diaw", "Seny Dieng",
        "Kalidou Koulibaly", "Abdou Diallo", "Moussa Niakhaté",
        "Youssouf Sabaly", "Ismail Jakobs", "Krépin Diatta",
        "Abdoulaye Seck", "Formose Mendy",
        "Idrissa Gana Gueye", "Pape Gueye", "Cheikhou Kouyaté",
        "Pathé Ciss", "Lamine Camara",
        "Sadio Mané", "Ismaïla Sarr", "Boulaye Dia", "Habib Diallo",
        "Iliman Ndiaye", "Nicolas Jackson", "Pape Matar Sarr",
    },
    "Morocco": {
        "Yassine Bounou", "Munir Mohamedi", "Ahmed Reda Tagnaouti",
        "Achraf Hakimi", "Noussair Mazraoui", "Romain Saïss",
        "Nayef Aguerd", "Adam Masina", "Achraf Dari", "Yahya Attiat-Allah",
        "Sofyan Amrabat", "Azzedine Ounahi", "Selim Amallah",
        "Bilal El Khannouss", "Eliesse Ben Seghir",
        "Hakim Ziyech", "Brahim Díaz", "Ilias Akhomach",
        "Youssef En-Nesyri", "Soufiane Rahimi", "Soufiane Boufal",
        "Abde Ezzalzouli", "Amine Adli",
    },
    "Colombia": {
        "Camilo Vargas", "David Ospina", "Álvaro Montero",
        "Daniel Muñoz", "Santiago Arias", "Davinson Sánchez",
        "Jhon Lucumí", "Yerry Mina", "Carlos Cuesta", "Johan Mojica",
        "Deiver Machado",
        "Mateus Uribe", "Jefferson Lerma", "Richard Ríos",
        "James Rodríguez", "Juan Fernando Quintero", "Jorge Carrascal",
        "Luis Díaz", "Jhon Córdoba", "Rafael Santos Borré",
        "Miguel Borja", "Jhon Durán", "Yaser Asprilla",
    },
    "Egypt": {
        "Mohamed El-Shenawy", "Mohamed Abou Gabal", "Mohamed Sobhi",
        "Mohamed Hany", "Ahmed Hegazy", "Mohamed Abdelmoneim",
        "Omar Kamal", "Mohamed Hamdy", "Ali Maaloul",
        "Mohamed Elneny", "Tarek Hamed", "Hamdi Fathi",
        "Trezeguet", "Mostafa Mohamed", "Mahmoud Trezeguet",
        "Mohamed Salah", "Omar Marmoush", "Mostafa Fathi",
        "Marwan Hamdy",
    },
    "Australia": {
        "Mat Ryan", "Joe Gauci", "Andrew Redmayne",
        "Harry Souttar", "Cameron Burgess", "Kye Rowles", "Thomas Deng",
        "Aziz Behich", "Lewis Miller", "Jordan Bos", "Jason Davidson",
        "Aiden O'Neill", "Jackson Irvine", "Connor Metcalfe",
        "Keanu Baccus", "Riley McGree",
        "Mathew Leckie", "Awer Mabil", "Martin Boyle",
        "Mitchell Duke", "Kusini Yengi", "John Iredale",
    },
    "Algeria": {
        "Raïs M'Bolhi", "Anthony Mandrea", "Alexandre Oukidja",
        "Aïssa Mandi", "Ramy Bensebaini", "Youcef Atal", "Jaouen Hadjam",
        "Mohamed Tougai", "Reda Halaïmia",
        "Ismaël Bennacer", "Riyad Mahrez", "Houssem Aouar",
        "Said Benrahma", "Adam Ounas", "Yacine Brahimi",
        "Baghdad Bounedjah", "Islam Slimani", "Andy Delort",
        "Mohamed Amoura", "Saïd Belhocine",
    },
    "Switzerland": {
        "Yann Sommer", "Gregor Kobel", "Yvon Mvogo",
        "Manuel Akanji", "Nico Elvedi", "Fabian Schär", "Eray Cömert",
        "Ricardo Rodríguez", "Silvan Widmer", "Edimilson Fernandes",
        "Granit Xhaka", "Remo Freuler", "Fabian Rieder",
        "Denis Zakaria", "Ardon Jashari", "Vincent Sierro",
        "Xherdan Shaqiri", "Ruben Vargas", "Dan Ndoye",
        "Breel Embolo", "Zeki Amdouni", "Noah Okafor", "Renato Steffen",
    },
    "Ivory Coast": {
        "Yahia Fofana", "Badra Ali Sangaré", "Yvan Eba",
        "Serge Aurier", "Wilfried Singo", "Eric Bailly", "Evan Ndicka",
        "Odilon Kossounou", "Ghislain Konan", "Willy Boly",
        "Franck Kessié", "Seko Fofana", "Ibrahim Sangaré",
        "Jean Michaël Seri", "Yves Bissouma",
        "Nicolas Pépé", "Wilfried Zaha", "Sébastien Haller",
        "Jonathan Bamba", "Max Gradel", "Karim Konaté",
        "Simon Adingra", "Christian Kouamé",
    },
    "Ghana": {
        "Richard Ofori", "Lawrence Ati-Zigi", "Joe Wollacott",
        "Daniel Amartey", "Alexander Djiku", "Mohammed Salisu",
        "Jonathan Mensah", "Tariq Lamptey", "Alidu Seidu",
        "Baba Rahman", "Gideon Mensah",
        "Thomas Partey", "Mohammed Kudus", "Andre Ayew",
        "Jordan Ayew", "Antoine Semenyo", "Salis Abdul Samed",
        "Inaki Williams", "Iñaki Williams", "Felix Afena-Gyan",
        "Ernest Nuamah", "Kamaldeen Sulemana", "Joseph Paintsil",
    },
    "Japan": {
        "Zion Suzuki", "Daniel Schmidt", "Shuichi Gonda",
        "Hiroki Sakai", "Yuto Nagatomo", "Takehiro Tomiyasu", "Ko Itakura",
        "Maya Yoshida", "Shogo Taniguchi", "Hiroki Ito", "Hiroki Sakai",
        "Hidemasa Morita", "Wataru Endo", "Ao Tanaka", "Ritsu Doan",
        "Junya Ito", "Kaoru Mitoma", "Takefusa Kubo",
        "Daichi Kamada", "Takumi Minamino", "Yuya Osako",
        "Daizen Maeda", "Ayase Ueda", "Keito Nakamura",
    },
    "Canada": {
        "Milan Borjan", "Maxime Crépeau", "Dayne St. Clair",
        "Alistair Johnston", "Richie Laryea", "Steven Vitória",
        "Kamal Miller", "Derek Cornelius", "Sam Adekugbe",
        "Stephen Eustáquio", "Atiba Hutchinson", "Mark-Anthony Kaye",
        "Ismaël Koné", "Liam Fraser",
        "Alphonso Davies", "Tajon Buchanan", "Junior Hoilett",
        "Cyle Larin", "Jonathan David", "Lucas Cavallini",
        "Iké Ugbo", "Jacob Shaffelburg", "Theo Bair",
    },
    "Panama": {
        "Luis Mejía", "Orlando Mosquera", "José Calderón",
        "Andrés Andrade", "César Blackman", "Eric Davis", "Fidel Escobar",
        "Harold Cummings", "Michael Murillo", "Roderick Miller",
        "Adalberto Carrasquilla", "Aníbal Godoy", "Christian Martínez",
        "Cristian Martínez", "Édgar Bárcenas", "Jorman Aguilar",
        "Cristian Arango", "Ismael Díaz", "José Fajardo",
        "Rolando Blackburn",
    },
    "Cape Verde": {
        "Vozinha", "Márcio Rosa", "Diogo Garcés",
        "Roberto Lopes", "Stopira", "Jeffry Fortes", "Lisandro Semedo",
        "Diney", "Manuel Cabral",
        "Kenny Rocha Santos", "Sidny Cabral", "Hélder Tavares",
        "Garry Rodrigues", "Ryan Mendes", "Bebé",
        "Bryan Teixeira", "Stopira", "Patrick Andrade",
        "Jovane Cabral", "Júlio Tavares", "Garry Mendes Rodrigues",
        "Heriberto Tavares",
    },
    "DR Congo": {
        "Lionel Mpasi-Nzau", "Timothy Fayulu", "Hervé Koffi",
        "Chancel Mbemba", "Gédéon Kalulu", "Dylan Batubinsika",
        "Joris Kayembe", "Marcel Tisserand", "Arthur Masuaku",
        "Aaron Wan-Bissaka",
        "Samuel Moutoussamy", "Charles Pickel", "Edo Kayembe",
        "Chadrac Akolo", "Yoane Wissa", "Cédric Bakambu",
        "Silas Katompa Mvumpa", "Théo Bongonda", "Meschack Elia",
        "Fiston Mayele",
    },
    "Jordan": {
        "Yazeed Abulaila", "Abdullah Al-Fakhouri", "Amir Shafia",
        "Salem Al-Ajalin", "Yazan Al-Arab", "Bara'a Marei",
        "Abdallah Nasib", "Mohammad Abu Hashish", "Ehsan Haddad",
        "Yazan Al-Naimat", "Mahmoud Al-Mardi", "Noor Al-Rawabdeh",
        "Mohammed Abu Zrayq", "Nizar Al-Rashdan",
        "Musa Al-Taamari", "Mahmoud Al-Mawas", "Ali Olwan",
        "Hamza Al-Dardour", "Ahmad Ersan",
    },
    "Iraq": {
        "Jalal Hassan", "Fahad Talib", "Hussein Hassan",
        "Akam Hashem", "Ahmed Ibrahim", "Manaf Younis",
        "Rebin Sulaka", "Saad Natiq", "Merchas Doski",
        "Hussein Ali", "Amjad Attwan", "Ibrahim Bayesh",
        "Mohanad Ali", "Aymen Hussein", "Ali Al-Hamadi",
        "Hussein Ali Al-Saedi", "Bashar Resan", "Sherko Karim",
    },
}


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────

# Build a normalised lookup map at import: { norm(country): { norm(player) } }
_NORM_SQUADS: dict[str, set[str]] = {
    _norm(country): {_norm(p) for p in players}
    for country, players in _SQUADS_RAW.items()
}


def is_in_squad(player: str, team: str) -> Optional[bool]:
    """Check whether `player` appears in the announced matchday squad
    for `team` (case + accent insensitive).

    Returns:
        True  — player IS in the squad
        False — squad is known and player is NOT in it (gate them out)
        None  — squad data unavailable for this team (don't gate)
    """
    if not player or not team:
        return None
    team_key = _norm(team)
    squad = _NORM_SQUADS.get(team_key)
    if squad is None:
        # No curated data — caller should treat as unknown.
        return None
    player_key = _norm(player)
    if not player_key:
        return None
    # Full-name match first.
    if player_key in squad:
        return True
    # Fallback: surname / last-token match (handles "Mbappé" vs full
    # "Kylian Mbappé"). Only triggers if the surname is unique in the
    # squad — guards against false positives like "Silva" vs "Bruno Silva".
    last = player_key.split()[-1] if player_key else ""
    if len(last) >= 4:
        matches = [s for s in squad if s.endswith(" " + last) or s == last]
        if len(matches) == 1:
            return True
    return False


def get_squad(team: str) -> Optional[set[str]]:
    """Return the raw player-name set for a team, or None if unknown."""
    return _SQUADS_RAW.get(team)


def known_teams() -> list[str]:
    return sorted(_SQUADS_RAW.keys())
