import re
from robobrowser import RoboBrowser
b = RoboBrowser()
b.addheaders = [('User-agent', 'Mozilla/5.0 (X11; U; Linux i686; en-US; rv:1.9.0.1) Gecko/2008071615 Fedora/3.0.1-1.fc9 Firefox/3.0.1')]

url = "https://findmyfbid.com/"
listid = "list-fb.txt"
#FORMAT 1 (URL) : https://www.facebook.com/aldi.katadji

try:
    listid = open(listid, "r")
except:
    print ("\nList Not Found!")
    quit()

for username in listid:
    if username.strip() == "":
        print("BARIS KOSONG")
    else:
        b.open(url)
        form = b.get_form()
        form['url'] = username.strip()
        b.method = "POST"
        b.submit_form(form)
        soup = str(b.parsed)
        
        def convert(s):
            str1 = ""
            return(str1.join(s))
        
        if b.response.status_code==200:
            s = re.findall(r'\d+', soup)
            print(convert(s))
        else:
            print("KONEKSI BURUK / WEBSITE TIDAK DAPAT DIAKSES")
print("============= D O N E ==============")

