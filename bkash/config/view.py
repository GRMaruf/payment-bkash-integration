from django.shortcuts import render

def test_home(request):
    return render(request, 'home.html')