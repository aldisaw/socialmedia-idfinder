import re
from robobrowser import RoboBrowser
b = RoboBrowser();
b.addheaders = [('User-agent', 'Mozilla/5.0 (X11; U; Linux i686; en-US; rv:1.9.0.1) Gecko/2008071615 Fedora/3.0.1-1.fc9 Firefox/3.0.1')]

url = "http://findmyfbid.in/find-my-twitter-id/"
listid = "list-tw.txt"
#FORMAT 1 (URL) : https://www.twitter.com/aldisaw/
#FORMAT 2 username : aldisaw

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
		form['handle'] = username.strip().replace("https://twitter.com/","").replace("/","")
		b.method = "POST"
		b.submit_form(form)
		
		src = str(b.parsed())
		
		start = '<code>'
		end = '</code>'

		result = re.search('%s(.+?)%s' % (start, end), src).group(1)
		print(result)
print("============= D O N E ==============")